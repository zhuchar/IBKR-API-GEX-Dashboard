import asyncio
import bisect
import math
from collections import defaultdict

from common import PRESET_SYMBOLS
import yfinance as yf

# Optional: Set a new event loop if not already in an async environment (e.g., in a notebook or certain frameworks)
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

import time
from ib_insync import *

async def main(callback,symbol,expiration,strikes_up,strikes_down):
    config = PRESET_SYMBOLS[symbol]

    ib = IB()
    try:
        await ib.connectAsync('127.0.0.1', 7496, clientId=12, timeout=1000, readonly=True)
        print("Connected")

        if config["secType"] == "IND":
            contract = Index(symbol, config["exchange"])
        elif config["secType"] == "FUT":
            contract = Future(symbol=symbol, lastTradeDateOrContractMonth=config["lastTradeDateOrContractMonth"], exchange=config["exchange"])
        else:
            contract = Stock(symbol, config["exchange"])

        await ib.qualifyContractsAsync(contract)
        ticker = ib.reqMktData(contract, '', snapshot=False, regulatorySnapshot=False)

        # Yahoo
        TICKER = "^" + symbol
        tickerY = yf.Ticker(TICKER)
        option_dataY = tickerY.option_chain(f"20{expiration[:2]}-{expiration[2:4]}-{expiration[4:]}")

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
            price = config["default_price"]
            callback("warning", f"⚠️ Using fallback price: ${price}")

        # Generate option symbols
        chains = await ib.reqSecDefOptParamsAsync(contract.symbol, config["exchange"] if config["secType"] == "FUT" else '', contract.secType, contract.conId)
        chain = [c for c in chains if c.exchange == config["exchange"] and c.tradingClass == config["option_prefix"]][0]

        # expiration (YYMMDD)
        if not "20"+expiration in chain.expirations:
            callback("error", f"⚠️ Invalid expiration: {expiration}, first expiration is {chain.expirations[0]}")
            time.sleep(2)

            ib.disconnect()
            print("Disconnected")
            return price, {}

        pos = bisect.bisect_left(chain.strikes, price)
        strikes = chain.strikes[pos-strikes_up:pos+strikes_down]

        option_contracts = []
        for strike in strikes:
            for right in ['C', 'P']:  # Calls and Puts
                if config['secType'] == 'FUT':
                    contract = FuturesOption(contract.symbol, "20" + expiration, strike, right, contract.exchange, tradingClass=config["option_prefix"])
                else:
                    contract = Option(contract.symbol, "20"+expiration, strike, right, contract.exchange, tradingClass=config["option_prefix"])
                option_contracts.append(contract)

        # Fetch option data
        callback("info", f"📡 Fetching data for {len(option_contracts)} options...")

        option_contracts = await ib.qualifyContractsAsync(*option_contracts)

        data = defaultdict(dict)
        size = 20
        l,r = 0,size if size<len(option_contracts) else len(option_contracts)
        while l<len(option_contracts):
            print(f"{l}...")
            tickers = []
            for contract in option_contracts[l:r]:
                ticker = ib.reqMktData(contract, '100,101', snapshot=False, regulatorySnapshot=False)
                tickers.append(ticker)

            await asyncio.sleep(3)

            for ticker in tickers:
                if ticker.bid is not None and ticker.ask is not None:
                    if ticker.modelGreeks is None:
                        # raise ValueError("No Greeks")
                        print(f"Warning: {ticker.contract.localSymbol} has no Greeks. last = {ticker.last}, market price = {ticker.marketPrice()}, bid = {ticker.bid}, ask = {ticker.ask}")
                        continue

                    # if (ticker.contract.right == "C" and math.isnan(ticker.callOpenInterest)) or (ticker.contract.right == "P" and math.isnan(ticker.putOpenInterest)):
                    #     print(f"Warning: {ticker.contract.localSymbol} open interest is nan. last = {ticker.last}, market price = {ticker.marketPrice()}, bid = {ticker.bid}, ask = {ticker.ask}, right = {ticker.contract.right}, callOI = {ticker.callOpenInterest}, putOI = {ticker.putOpenInterest}")
                    #     continue

                    # .{PREFIX}{YYMMDD}{C | P}{STRIKE}, eg. .SPXW251219C6000
                    exp = ticker.contract.lastTradeDateOrContractMonth[2:]
                    right = ticker.contract.right
                    right_index = ticker.contract.localSymbol.rfind("C") if right == "C" else ticker.contract.localSymbol.rfind("P")
                    strike = ticker.contract.localSymbol[right_index+1:]
                    key = f".{config["option_prefix"]}{exp}{right}{ticker.contract.strike}"
                    keyY = f"{config["option_prefix"]}{exp}{right}{strike}"

                    # print(key, ticker.modelGreeks)

                    data[key]["gamma"] = ticker.modelGreeks.gamma
                    data[key]["delta"] = ticker.modelGreeks.delta
                    data[key]["iv"] = ticker.modelGreeks.impliedVol
                    if ticker.contract.right == "C":
                        # data[key]["oi"] = ticker.callOpenInterest
                        data[key]["oi"] = list((option_dataY.calls.loc[option_dataY.calls['contractSymbol'] == keyY])['openInterest'])[0]

                    else:
                        # data[key]["oi"] = ticker.putOpenInterest
                        data[key]["oi"] = list((option_dataY.puts.loc[option_dataY.puts['contractSymbol'] == keyY])['openInterest'])[0]
                    if data[key]["oi"] == 0:
                        print(f"Warning: {ticker.contract.localSymbol} open interest is 0")
                    data[key]["volume"] = ticker.volume

            for contract in option_contracts[l:r]:
                ib.cancelMktData(contract)

            await asyncio.sleep(0.5)

            l += size
            r = r+size if r+size<len(option_contracts) else len(option_contracts)

        ib.disconnect()
        print("Disconnected")

        return price, data

    except Exception as e:
        if ib.isConnected():
            ib.disconnect()
        raise e

def fetch_option_data(callback,symbol,expiration,strikes_up,strikes_down):
    loop = None
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError as ex:
        if "There is no current event loop in thread" in str(ex):
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        else:
            raise

    price, option_contracts = loop.run_until_complete(main(callback,symbol,expiration,strikes_up,strikes_down))
    return price, option_contracts