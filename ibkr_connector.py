import asyncio
import bisect
import math

# Optional: Set a new event loop if not already in an async environment (e.g., in a notebook or certain frameworks)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

import time
from ib_insync import *

async def main(callback, symbol,option_prefix,expiration,strikes_up,strikes_down):
    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 7496, clientId=2, timeout=1000, readonly=True)
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
            await asyncio.sleep(0.1)
            # ib.sleep(1) # Sleep allows the ib_insync loop to process messages

            if not math.isnan(ticker.last):
                price = ticker.last
                break
        ib.cancelMktData(contract)

        if price:
            callback("success", f"✅ {symbol} Price: ${price:,.2f}")
        else:
            # TODO: hardcode default price
            price = 6836
            callback("warning", f"⚠️ Using fallback price: ${price}")

        # Generate option symbols
        chains = await ib.reqSecDefOptParamsAsync(contract.symbol, '', contract.secType, contract.conId)
        # TODO: use option_prefix not SPXW hardcoding
        chain = [c for c in chains if c.exchange == 'CBOE' and c.tradingClass == 'SPXW'][0]

        # expiration (YYMMDD)
        if not "20"+expiration in chain.expirations:
            callback("error", f"⚠️ Invalid expiration: {expiration}, first expiration is {chain.expirations[0]}")
            time.sleep(2)
            return price, {}

        pos = bisect.bisect_left(chain.strikes, price)
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
        data = {}
        for contract in option_contracts:
            ticker = ib.reqMktData(contract, '100,101', snapshot=False, regulatorySnapshot=False)
            while ticker.last is None and ticker.marketPrice() is None:
                await asyncio.sleep(0.1)

            if ticker.bid is not None and ticker.ask is not None:
                if ticker.modelGreeks is None:
                    # raise ValueError("No Greeks")
                    print(f"Warning: {ticker.contract.localSymbol} has no Greeks. last = {ticker.last}, market price = {ticker.marketPrice()}, bid = {ticker.bid}, ask = {ticker.ask}")
                    continue

                # TODO: use option_prefix not SPXW hardcoding
                # .{PREFIX}{YYMMDD}{C | P}{STRIKE}, eg. .SPXW251219C6000
                exp = ticker.contract.lastTradeDateOrContractMonth[2:]
                key = f".SPXW{exp}{ticker.contract.right}{ticker.contract.strike}"
                if key not in data:
                    data[key] = {}

                data[key]["gamma"] = ticker.modelGreeks.gamma
                data[key]["delta"] = ticker.modelGreeks.delta
                data[key]["iv"] = ticker.modelGreeks.impliedVol
                data[key]["oi"] = ticker.callOpenInterest + ticker.putOpenInterest
                data[key]["volume"] = ticker.volume

            ib.cancelMktData(contract)
            await asyncio.sleep(0.1)

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