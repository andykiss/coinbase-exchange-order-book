import asyncio
from datetime import datetime
from decimal import Decimal
from trading import file_logger

try:
    import ujson as json
except ImportError:
    import json

import logging
from pprint import pformat
import random
from socket import gaierror
import sys
import time

from dateutil.tz import tzlocal
import requests
import websockets

from trading.exchange import exchange_api_url, exchange_auth
from trading.openorders import OpenOrders
from trading.spreads import Spreads
from orderbook.book import Book

command_line = False

order_book = Book()
open_orders = OpenOrders()
open_orders.cancel_all()
spreads = Spreads()


@asyncio.coroutine
def websocket_to_order_book():

    try:
        coinbase_websocket = yield from websockets.connect("wss://ws-feed.exchange.coinbase.com")
    except gaierror:
        file_logger.error('socket.gaierror - had a problem connecting to Coinbase feed')
        return

    yield from coinbase_websocket.send('{"type": "subscribe", "product_id": "BTC-USD"}')

    messages = []
    while True:
        message = yield from coinbase_websocket.recv()
        message = json.loads(message)
        messages += [message]
        if len(messages) > 20:
            break

    order_book.get_level3()
    open_orders.get_open_orders()

    [order_book.process_message(message) for message in messages if message['sequence'] > order_book.level3_sequence]

    while True:
        message = yield from coinbase_websocket.recv()
        if message is None:
            file_logger.error('Websocket message is None.')
            return False
        try:
            message = json.loads(message)
        except TypeError:
            file_logger.error('JSON did not load, see ' + str(message))
            return False
        if not order_book.process_message(message):
            print(pformat(message))
            return False
        max_bid = Decimal(order_book.bids.price_tree.max_key())
        min_ask = Decimal(order_book.asks.price_tree.min_key())
        print('Latency: {0:.6f} secs, '
              'Min ask: {1:.2f}, Max bid: {2:.2f}, Spread: {3:.2f}'.format(
            ((datetime.now(tzlocal()) - order_book.last_time).microseconds * 1e-6),
            min_ask, max_bid, min_ask - max_bid), end='\r')
        # if not manage_orders():
        #     print(pformat(message))
        #     return False


def manage_orders():
    max_bid = Decimal(order_book.bids.price_tree.max_key())
    min_ask = Decimal(order_book.asks.price_tree.min_key())
    if min_ask - max_bid < 0:
        file_logger.warn('Negative spread: {0}'.format(min_ask - max_bid))
        return False
    if command_line:
        print('Latency: {0:.6f} secs, '
              'Min ask: {1:.2f}, Max bid: {2:.2f}, Spread: {3:.2f}, '
              'Your ask: {4:.2f}, Your bid: {5:.2f}, Your spread: {6:.2f}'.format(
            ((datetime.now(tzlocal()) - order_book.last_time).microseconds * 1e-6),
            min_ask, max_bid, min_ask - max_bid,
            open_orders.float_open_ask_price, open_orders.float_open_bid_price,
                              open_orders.float_open_ask_price - open_orders.float_open_bid_price), end='\r')

    if not open_orders.open_bid_order_id and not open_orders.insufficient_usd:
        if open_orders.insufficient_btc:
            size = 0.1
            open_bid_price = Decimal(round(max_bid + Decimal(open_orders.open_bid_rejections), 2))
        else:
            size = 0.01
            spreads.bid_spread = Decimal(round((random.randrange(15) + 6) / 100, 2))
            open_bid_price = Decimal(round(min_ask - Decimal(spreads.bid_spread)
                                           - Decimal(open_orders.open_bid_rejections), 2))
        order = {'size': size,
                 'price': str(open_bid_price),
                 'side': 'buy',
                 'product_id': 'BTC-USD',
                 'post_only': True}
        response = requests.post(exchange_api_url + 'orders', json=order, auth=exchange_auth)
        if 'status' in response.json() and response.json()['status'] == 'pending':
            open_orders.open_bid_order_id = response.json()['id']
            open_orders.open_bid_price = open_bid_price
            open_orders.open_bid_rejections = 0.0
            file_logger.info('new bid @ {0}'.format(open_bid_price))
        elif 'status' in response.json() and response.json()['status'] == 'rejected':
            open_orders.open_bid_order_id = None
            open_orders.open_bid_price = None
            open_orders.open_bid_rejections += 0.04
            file_logger.warn('rejected: new bid @ {0}'.format(open_bid_price))
        elif 'message' in response.json() and response.json()['message'] == 'Insufficient funds':
            open_orders.insufficient_usd = True
            open_orders.open_bid_order_id = None
            open_orders.open_bid_price = None
            file_logger.warn('Insufficient USD')
        else:
            file_logger.error('Unhandled response: {0}'.format(pformat(response.json())))
            return False
        return True

    if not open_orders.open_ask_order_id and not open_orders.insufficient_btc:
        if open_orders.insufficient_usd:
            size = 0.10
            open_ask_price = Decimal(round(min_ask + Decimal(open_orders.open_ask_rejections), 2))
        else:
            size = 0.01
            spreads.ask_spread = Decimal(round((random.randrange(15) + 6) / 100, 2))
            open_ask_price = Decimal(round(max_bid + Decimal(spreads.ask_spread)
                                           + Decimal(open_orders.open_ask_rejections), 2))
        order = {'size': size,
                 'price': str(open_ask_price),
                 'side': 'sell',
                 'product_id': 'BTC-USD',
                 'post_only': True}
        response = requests.post(exchange_api_url + 'orders', json=order, auth=exchange_auth)
        if 'status' in response.json() and response.json()['status'] == 'pending':
            open_orders.open_ask_order_id = response.json()['id']
            open_orders.open_ask_price = open_ask_price
            file_logger.info('new ask @ {0}'.format(open_ask_price))
            open_orders.open_ask_rejections = 0
        elif 'status' in response.json() and response.json()['status'] == 'rejected':
            open_orders.open_ask_order_id = None
            open_orders.open_ask_price = None
            open_orders.open_ask_rejections += 0.04
            file_logger.warn('rejected: new ask @ {0}'.format(open_ask_price))
        elif 'message' in response.json() and response.json()['message'] == 'Insufficient funds':
            open_orders.insufficient_btc = True
            open_orders.open_ask_order_id = None
            open_orders.open_ask_price = None
            file_logger.warn('Insufficient BTC')
        else:
            file_logger.error('Unhandled response: {0}'.format(pformat(response.json())))
            return False
        return True

    if open_orders.open_bid_order_id and Decimal(open_orders.open_bid_price) < round(
                    min_ask - Decimal(spreads.bid_adjustment_spread), 2):
        file_logger.info('CANCEL: open bid {0} threshold {1} diff {2}'.format(
            Decimal(open_orders.open_bid_price),
            round(min_ask - Decimal(spreads.bid_adjustment_spread), 2),
            Decimal(open_orders.open_bid_price) - round(min_ask - Decimal(spreads.bid_adjustment_spread), 2)))
        open_orders.cancel('bid')
        return True

    if open_orders.open_ask_order_id and Decimal(open_orders.open_ask_price) > round(
                    max_bid + Decimal(spreads.ask_adjustment_spread), 2):
        file_logger.info('CANCEL: open ask {0} threshold {1} diff {2}'.format(
            Decimal(open_orders.open_ask_price),
            round(max_bid - Decimal(spreads.ask_adjustment_spread), 2),
            Decimal(open_orders.open_ask_price) - round(max_bid + Decimal(spreads.ask_adjustment_spread), 2)))
        open_orders.cancel('ask')
        return True
    return True

if __name__ == '__main__':
    if len(sys.argv) == 1:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(logging.Formatter('\n%(asctime)s, %(levelname)s, %(message)s'))
        stream_handler.setLevel(logging.INFO)
        file_logger.addHandler(stream_handler)
        command_line = True

    loop = asyncio.get_event_loop()
    n = 0
    while True:
        start_time = loop.time()
        loop.run_until_complete(websocket_to_order_book())
        end_time = loop.time()
        seconds = end_time - start_time
        if seconds < 2:
            n += 1
            sleep_time = (2 ** n) + (random.randint(0, 1000) / 1000)
            file_logger.error('Websocket connectivity problem, going to sleep for {0}'.format(sleep_time))
            time.sleep(sleep_time)
            if n > 6:
                n = 0
