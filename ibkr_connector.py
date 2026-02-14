import asyncio
import bisect

# Optional: Set a new event loop if not already in an async environment (e.g., in a notebook or certain frameworks)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

import time
from ib_insync import *

async def main(callback, symbol,option_prefix,expiration,strikes_up,strikes_down):
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 7496, clientId=10, timeout=1000, readonly=True)
        print("Connected")

        # TODO: use symbol not SPX hardcoding
        contract = Index('SPX', 'CBOE')

        await ib.qualifyContractsAsync(contract)
        ticker = ib.reqMktData(contract, '', snapshot=False, regulatorySnapshot=False)

        # Get underlying price
        callback("info",f"📊 Getting {symbol} price...")

        price = None
        end_time = time.time() + 5
        while time.time() < end_time:
            await asyncio.sleep(1)
            # ib.sleep(1) # Sleep allows the ib_insync loop to process messages

            if ticker.last is not None:
                price = ticker.last
                # print(f"Last price: {ticker.last}, Bid: {ticker.bid}, Ask: {ticker.ask}")

        # Generate option symbols
        chains = await ib.reqSecDefOptParamsAsync(contract.symbol, '', contract.secType, contract.conId)
        chain = chains[0]

        # expiration (YYMMDD)
        if not "20"+expiration in chain.expirations:
            raise ValueError("Invalid expiration: " + expiration + ", first expiration is " + chain.expirations[0])

        if price:
            callback("success", f"✅ {symbol} Price: ${price:,.2f}")
        else:
            price = 6000
            callback("warning", f"⚠️ Using fallback price: ${price}")

        pos = bisect.bisect_left(chain.strikes, 6000)
        strikes = chain.strikes[pos-strikes_up:pos+strikes_down]

        option_contracts = []
        for strike in strikes:
            for right in ['C', 'P']:  # Calls and Puts
                # TODO: use option_prefix not SPXW hardcoding
                contract = Option(contract.symbol, "20"+expiration, strike, right, contract.exchange, tradingClass='SPXW')
                option_contracts.append(contract)

        # Fetch option data
        callback("info", f"📡 Fetching data for {len(option_contracts)} options...")

        option_contracts = await ib.qualifyContractsAsync(*option_contracts)
        tickers = []
        for contract in option_contracts:
            ticker = ib.reqMktData(contract, '100,101', snapshot=False, regulatorySnapshot=False)
            tickers.append(ticker)

        await asyncio.sleep(2)

        # Process the results
        data = {}
        for ticker in tickers:
            if ticker.bid is not None and ticker.ask is not None:
                if ticker.modelGreeks is None:
                    raise ValueError("No Greeks")

                # TODO: use option_prefix not SPXW hardcoding
                # .{PREFIX}{YYMMDD}{C | P}{STRIKE}, eg. .SPXW251219C6000
                key = f".SPXW{ticker.contract.localSymbol}"
                if key not in data:
                    data[key] = {}

                data[key]["gamma"] = ticker.modelGreeks.gamma
                data[key]["delta"] = ticker.modelGreeks.delta
                data[key]["iv"] = ticker.modelGreeks.impliedVol
                data[key]["oi"] = ticker.callOpenInterest + ticker.putOpenInterest
                data[key]["volume"] = ticker.volume

        # ib.cancelMktData(ticker)
        # ib.cancelMktData(contract)
        ib.disconnect()

        return price, data

    except Exception as e:
        if ib.isConnected():
            ib.disconnect()
        raise e

def fetch_option_data(callback, symbol,option_prefix,expiration,strikes_up,strikes_down):
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError as ex:
        if "There is no current event loop in thread" in str(ex):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        else:
            raise

    price, option_contracts = loop.run_until_complete(main(callback,symbol,option_prefix,expiration,strikes_up,strikes_down))
    return price, option_contracts