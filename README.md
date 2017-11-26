### Python Adapter for BitMEX Realtime Data

This is a reference adapter for receiving realtime data from the BitMEX API. See the source for documentation.

### Usage

Edit main.py to include your credentials. I recommend starting out on the
[Testnet](https://testnet.bitmex.com).

```bash
[username@hostname]$ python
Python 2.7.14 (default, Sep 22 2017, 15:49:07) 
[GCC 4.4.7 20120313 (Red Hat 4.4.7-18)] on linux2
Type "help", "copyright", "credits" or "license" for more information.
>>>
>>> import logging
>>> from bitmex_api import BitMEXWebsocket
>>>
>>> ws = BitMEXWebsocket(endpoint="https://testnet.bitmex.com/api/v1", symbol="XBTUSD",api_key="your_key",api_secret="your_secret")
>>>
>>> logger = logging.getLogger()
>>>
>>> logger.setLevel(logging.INFO)
>>> 
>>> logger.info(dir(ws))

```



