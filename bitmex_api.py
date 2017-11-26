import sys
import requests
import base64
import websocket
import uuid
import threading
import traceback
import time
from time import sleep
from auth import APIKeyAuthWithExpires
import json
import string
import logging
import urlparse
import math
from util.actual_kwargs import actual_kwargs
from util.api_key import generate_nonce, generate_signature


# Naive implementation of connecting to BitMEX websocket for streaming realtime data.
# The Marketmaker still interacts with this as if it were a REST Endpoint, but now it can get
# much more realtime data without polling the hell out of the API.
#
# The Websocket offers a bunch of data as raw properties right on the object.
# On connect, it synchronously asks for a push of all this data then returns.
# Right after, the MM can start using its data. It will be updated in realtime, so the MM can
# poll really often if it wants.
class BitmexAPI():

    # Don't grow a table larger than this amount. Helps cap memory usage.
    MAX_TABLE_LEN = 2000

    # We use the actual_kwargs decorator to get all kwargs sent to this method so we can easily pass
    # it to a validator function.
    @actual_kwargs()
    def __init__(self, endpoint=None, symbol=None, api_key=None, api_secret=None, orderIDPrefix='0z4ck_bitmex_'):
        '''Connect to the websocket and initialize data stores.'''
        self.logger = logging.getLogger(__name__)
        self.logger.debug("Initializing WebSocket.")

        self.orderIDPrefix = orderIDPrefix
        self.symbol = symbol
        self.base_url = endpoint
        self.apiKey = api_key
        self.apiSecret = api_secret

        self.__validate(self.__init__.actual_kwargs)
        self.__reset(self.__init__.actual_kwargs)

        # We can subscribe right in the connection querystring, so let's build that.
        # Subscribe to all pertinent endpoints
        wsURL = self.__get_url()
        self.logger.info("Connecting to %s" % wsURL)
        self.__connect(wsURL, symbol)
        self.logger.info('Connected to WS.')

        self.session = requests.Session()
        # These headers are always sent
        self.session.headers.update({'user-agent': 'liquidbot-python/2.10.0'})
        self.session.headers.update({'content-type': 'application/json'})
        self.session.headers.update({'accept': 'application/json'})
        # Connected. Wait for partials
        self.__wait_for_symbol(symbol)
        if api_key:
            self.__wait_for_account()
        self.logger.info('Got all market data. Starting.')

    def exit(self):
        '''Call this to exit - will close websocket.'''
        self.exited = True
        self.ws.close()

    def get_instrument(self):
        '''Get the raw instrument data for this symbol.'''
        # Turn the 'tickSize' into 'tickLog' for use in rounding
        instrument = self.data['instrument'][0]
        instrument['tickLog'] = int(math.fabs(math.log10(instrument['tickSize'])))
        return instrument

    def get_ticker(self):
        '''Return a ticker object. Generated from quote and trade.'''
        lastQuote = self.data['quote'][-1]
        lastTrade = self.data['trade'][-1]
        ticker = {
            "last": lastTrade['price'],
            "buy": lastQuote['bidPrice'],
            "sell": lastQuote['askPrice'],
            "mid": (float(lastQuote['bidPrice'] or 0) + float(lastQuote['askPrice'] or 0)) / 2
        }

        # The instrument has a tickSize. Use it to round values.
        instrument = self.data['instrument'][0]
        return {k: round(float(v or 0), instrument['tickLog']) for k, v in ticker.iteritems()}

    def funds(self):
        '''Get your margin details.'''
        return self.data['margin'][0]

    def market_depth(self):
        '''Get market depth (orderbook). Returns all levels.'''
        return self.data['orderBookL2']

    def open_orders(self, clOrdIDPrefix):
        '''Get all your open orders.'''
        orders = self.data['order']
        # Filter to only open orders (leavesQty > 0) and those that we actually placed
        return [o for o in orders if str(o['clOrdID']).startswith(clOrdIDPrefix) and o['leavesQty'] > 0]

    def recent_trades(self):
        '''Get recent trades.'''
        return self.data['trade']

    #
    # End Public Methods
    #

    def __connect(self, wsURL, symbol):
        '''Connect to the websocket in a thread.'''
        self.logger.debug("Starting thread")

        self.ws = websocket.WebSocketApp(wsURL,
                                         on_message=self.__on_message,
                                         on_close=self.__on_close,
                                         on_open=self.__on_open,
                                         on_error=self.__on_error,
                                         header=self.__get_auth())

        self.wst = threading.Thread(target=lambda: self.ws.run_forever())
        self.wst.daemon = True
        self.wst.start()
        self.logger.debug("Started thread")

        # Wait for connect before continuing
        conn_timeout = 5
        while not self.ws.sock or not self.ws.sock.connected and conn_timeout:
            sleep(1)
            conn_timeout -= 1
        if not conn_timeout:
            self.logger.error("Couldn't connect to WS! Exiting.")
            self.exit()
            sys.exit(1)

    def __get_auth(self):
        '''Return auth headers. Will use API Keys if present in settings.'''
        if self.config['api_key']:
            self.logger.info("Authenticating with API Key.")
            # To auth to the WS using an API key, we generate a signature of a nonce and
            # the WS API endpoint.
            nonce = generate_nonce()
            return [
                "api-nonce: " + str(nonce),
                "api-signature: " + generate_signature(self.config['api_secret'], 'GET', '/realtime', nonce, ''),
                "api-key:" + self.config['api_key']
            ]
        else:
            self.logger.info("Not authenticating.")
            return []

    def __get_url(self):
        '''
        Generate a connection URL. We can define subscriptions right in the querystring.
        Most subscription topics are scoped by the symbol we're listening to.
        '''

        # You can sub to orderBookL2 for all levels, or orderBook10 for top 10 levels & save bandwidth
        symbolSubs = ["execution", "instrument", "order", "orderBookL2", "position", "quote", "trade"]
        genericSubs = ["margin"]

        subscriptions = [sub + ':' + self.config['symbol'] for sub in symbolSubs]
        subscriptions += genericSubs

        urlParts = list(urlparse.urlparse(self.config['endpoint']))
        urlParts[0] = urlParts[0].replace('http', 'ws')
        urlParts[2] = "/realtime?subscribe=" + string.join(subscriptions, ",")
        return urlparse.urlunparse(urlParts)

    def __wait_for_account(self):
        '''On subscribe, this data will come down. Wait for it.'''
        # Wait for the keys to show up from the ws
        while not {'margin', 'position', 'order', 'orderBookL2'} <= set(self.data):
            sleep(0.1)

    def __wait_for_symbol(self, symbol):
        '''On subscribe, this data will come down. Wait for it.'''
        while not {'instrument', 'trade', 'quote'} <= set(self.data):
            sleep(0.1)

    def __send_command(self, command, args=[]):
        '''Send a raw command.'''
        self.ws.send(json.dumps({"op": command, "args": args}))


    def __on_message(self, ws, message):
        '''Handler for parsing WS messages.'''
        message = json.loads(message)
        self.logger.debug(json.dumps(message))

        self.debm =  message
        table = message['table'] if 'table' in message else None
        action = message['action'] if 'action' in message else None
        try:
            if 'subscribe' in message:
                self.logger.debug("Subscribed to %s." % message['subscribe'])
                pass
            elif action:

                if table not in self.data:
                    self.data[table] = []

                # There are four possible actions from the WS:
                # 'partial' - full table image
                # 'insert'  - new row
                # 'update'  - update row
                # 'delete'  - delete row
                if action == 'partial':
                    self.logger.debug("%s: partial" % table)
                    self.data[table] += message['data']
                    # Keys are communicated on partials to let you know how to uniquely identify
                    # an item. We use it for updates.
                    self.keys[table] = message['keys']
                elif action == 'insert':
                    self.logger.debug('%s: inserting %s' % (table, message['data']))
                    self.data[table] += message['data']

                    # Limit the max length of the table to avoid excessive memory usage.
                    # Don't trim orders because we'll lose valuable state if we do.
                    if len(self.data[table]) > BitmexAPI.MAX_TABLE_LEN:
                        self.data[table] = self.data[table][(BitmexAPI.MAX_TABLE_LEN / 2):]

                elif action == 'update':
                    self.logger.debug('%s: updating %s' % (table, message['data']))
                    # Locate the item in the collection and update it.
                    for updateData in message['data']:
                        item = findItemByKeys(self.keys[table], self.data[table], updateData)
                        if not item:
                            return  # No item found to update. Could happen before push
                        item.update(updateData)
                        # Remove cancelled / filled orders
                        if table == 'order' and item['leavesQty'] <= 0:
                            self.data[table].remove(item)
                elif action == 'delete':
                    self.logger.debug('%s: deleting %s' % (table, message['data']))
                    # Locate the item in the collection and remove it.
                    for deleteData in message['data']:
                        item = findItemByKeys(self.keys[table], self.data[table], deleteData)
                        self.data[table].remove(item)
                else:
                    raise Exception("Unknown action: %s" % action)
        except:
            self.logger.error(traceback.format_exc())
            pass

    def __on_error(self, ws, error):
        '''Called on fatal websocket errors. We exit on these.'''
        if not self.exited:
            self.logger.error("Error : %s" % error)
            sys.exit(1)

    def __on_open(self, ws):
        '''Called when the WS opens.'''
        self.logger.debug("Websocket Opened.")

    def __on_close(self, ws):
        '''Called on websocket close.'''
        self.logger.info('Websocket Closed')
        sys.exit(1)

    def __validate(self, kwargs):
        '''Simple method that ensure the user sent the right args to the method.'''
        if 'symbol' not in kwargs:
            self.logger.error("A symbol must be provided to BitmexAPI()")
            sys.exit(1)
        if 'endpoint' not in kwargs:
            self.logger.error("An endpoint (BitMEX URL) must be provided to BitmexAPI()")
            sys.exit(1)
        if 'api_key' not in kwargs:
            self.logger.error("No authentication provided! Unable to connect.")
            sys.exit(1)

    def __reset(self, kwargs):
        '''Resets internal datastores.'''
        self.data = {}
        self.keys = {}
        self.config = kwargs
        self.exited = False

    def buy(self, quantity, price):
        """Place a buy order.

        Returns order object. ID: orderID
        """
        return self.place_order(quantity, price)

    def sell(self, quantity, price):
        """Place a sell order.

        Returns order object. ID: orderID
        """
        return self.place_order(-quantity, price)

    def cancel(self, orderID):
        """Cancel an existing order."""
        path = "/order"
        postdict = {
            'orderID': orderID,
        }
        return self._curl_bitmex(path=path, postdict=postdict, verb="DELETE")

    def place_order(self, quantity, price):
        """Place an order."""
        if price < 0:
            raise Exception("Price must be positive.")

        endpoint = "/order"
        # Generate a unique clOrdID with our prefix so we can identify it.
        clOrdID = self.orderIDPrefix + base64.b64encode(uuid.uuid4().bytes).decode('utf8').rstrip('=\n')
        postdict = {
            'symbol': self.symbol,
            'orderQty': quantity,
            'price': price,
            'clOrdID': clOrdID
        }
        return self._curl_bitmex(path=endpoint, postdict=postdict, verb="POST")

    def _curl_bitmex(self, path, query=None, postdict=None, timeout=5, verb=None, rethrow_errors=False,
                     max_retries=None):
        """Send a request to BitMEX Servers."""
        # Handle URL
        url = self.base_url + path

        # Default to POST if data is attached, GET otherwise
        if not verb:
            verb = 'POST' if postdict else 'GET'

        # By default don't retry POST or PUT. Retrying GET/DELETE is okay because they are idempotent.
        # In the future we could allow retrying PUT, so long as 'leavesQty' is not used (not idempotent),
        # or you could change the clOrdID (set {"clOrdID": "new", "origClOrdID": "old"}) so that an amend
        # can't erroneously be applied twice.
        if max_retries is None:
            max_retries = 0 if verb in ['POST', 'PUT'] else 3

        # Auth: API Key/Secret
        auth = APIKeyAuthWithExpires.APIKeyAuthWithExpires(self.apiKey, self.apiSecret)

        def exit_or_throw(e):
            if rethrow_errors:
                raise e
            else:
                exit(1)

        def retry():
            self.retries += 1
            if self.retries > max_retries:
                raise Exception("Max retries on %s (%s) hit, raising." % (path, json.dumps(postdict or '')))
            return self._curl_bitmex(path, query, postdict, timeout, verb, rethrow_errors, max_retries)

        # Make the request
        response = None
        try:
            self.logger.info("sending req to %s: %s" % (url, json.dumps(postdict or query or '')))
            req = requests.Request(verb, url, json=postdict, auth=auth, params=query)
            prepped = self.session.prepare_request(req)
            response = self.session.send(prepped, timeout=timeout)
            # Make non-200s throw
            response.raise_for_status()

        except requests.exceptions.HTTPError as e:
            if response is None:
                raise e

            # 401 - Auth error. This is fatal.
            if response.status_code == 401:
                self.logger.error("API Key or Secret incorrect, please check and restart.")
                self.logger.error("Error: " + response.text)
                if postdict:
                    self.logger.error(postdict)
                    pass
                # Always exit, even if rethrow_errors, because this is fatal
                exit(1)

            # 404, can be thrown if order canceled or does not exist.
            elif response.status_code == 404:
                if verb == 'DELETE':
                    self.logger.error("Order not found: %s" % postdict['orderID'])
                    return
                self.logger.error("Unable to contact the BitMEX API (404). " +
                                  "Request: %s \n %s" % (url, json.dumps(postdict)))
                exit_or_throw(e)

            # 429, ratelimit; cancel orders & wait until X-Ratelimit-Reset
            elif response.status_code == 429:
                self.logger.error("Ratelimited on current request. Sleeping, then trying again. Try fewer " +
                                  "order pairs or contact support@bitmex.com to raise your limits. " +
                                  "Request: %s \n %s" % (url, json.dumps(postdict)))

                # Figure out how long we need to wait.
                ratelimit_reset = response.headers['X-Ratelimit-Reset']
                to_sleep = int(ratelimit_reset) - int(time.time())
                reset_str = datetime.datetime.fromtimestamp(int(ratelimit_reset)).strftime('%X')

                # We're ratelimited, and we may be waiting for a long time. Cancel orders.
                self.logger.warning("Canceling all known orders in the meantime.")
                self.cancel([o['orderID'] for o in self.open_orders()])

                self.logger.error("Your ratelimit will reset at %s. Sleeping for %d seconds." % (reset_str, to_sleep))
                sleep(to_sleep)

                # Retry the request.
                return retry()

            # 503 - BitMEX temporary downtime, likely due to a deploy. Try again
            elif response.status_code == 503:
                self.logger.warning("Unable to contact the BitMEX API (503), retrying. " +
                                    "Request: %s \n %s" % (url, json.dumps(postdict)))
                time.sleep(3)
                return retry()

            elif response.status_code == 400:
                error = response.json()['error']
                message = error['message'].lower() if error else ''

                # Duplicate clOrdID: that's fine, probably a deploy, go get the order(s) and return it
                if 'duplicate clordid' in message:
                    orders = postdict['orders'] if 'orders' in postdict else postdict

                    IDs = json.dumps({'clOrdID': [order['clOrdID'] for order in orders]})
                    orderResults = self._curl_bitmex('/order', query={'filter': IDs}, verb='GET')

                    for i, order in enumerate(orderResults):
                        if (
                                order['orderQty'] != abs(postdict['orderQty']) or
                                order['side'] != ('Buy' if postdict['orderQty'] > 0 else 'Sell') or
                                order['price'] != postdict['price'] or
                                order['symbol'] != postdict['symbol']):
                            raise Exception('Attempted to recover from duplicate clOrdID, but order returned from API ' +
                                            'did not match POST.\nPOST data: %s\nReturned order: %s' % (
                                                json.dumps(orders[i]), json.dumps(order)))
                    # All good
                    return orderResults

                elif 'insufficient available balance' in message:
                    self.logger.error('Account out of funds. The message: %s' % error['message'])
                    exit_or_throw(Exception('Insufficient Funds'))


            # If we haven't returned or re-raised yet, we get here.
            self.logger.error("Unhandled Error: %s: %s" % (e, response.text))
            self.logger.error("Endpoint was: %s %s: %s" % (verb, path, json.dumps(postdict)))
            exit_or_throw(e)

        except requests.exceptions.Timeout as e:
            # Timeout, re-run this request
            self.logger.warning("Timed out on request: %s (%s), retrying..." % (path, json.dumps(postdict or '')))
            return retry()

        except requests.exceptions.ConnectionError as e:
            self.logger.warning("Unable to contact the BitMEX API (%s). Please check the URL. Retrying. " +
                                "Request: %s %s \n %s" % (e, url, json.dumps(postdict)))
            sleep(1)
            return retry()

        # Reset retry counter on success
        self.retries = 0

        return response.json()

# Utility method for finding an item in the store.
# When an update comes through on the websocket, we need to figure out which item in the array it is
# in order to match that item.
#
# Helpfully, on a data push (or on an HTTP hit to /api/v1/schema), we have a "keys" array. These are the
# fields we can use to uniquely identify an item. Sometimes there is more than one, so we iterate through all
# provided keys.
def findItemByKeys(keys, table, matchData):
    for item in table:
        matched = True
        for key in keys:
            if item[key] != matchData[key]:
                matched = False
        if matched:
            return item
