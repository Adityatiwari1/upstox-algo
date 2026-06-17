# Upstox API Reference — Integration Guide

## Base URLs
```
Production:  https://api.upstox.com/v2/
HFT Orders:  https://api-hft.upstox.com/v2/
Sandbox:     https://sandbox.upstox.com/v2/
```

## Authentication
All requests require:
```
Authorization: Bearer {access_token}
Accept: application/json
```

Two token types:
- **access_token** — daily refresh required (OAuth TOTP flow)
- **analytics_token** — one-time generation, no daily re-auth

---

## WebSocket Market Data Feed

### Step 1 — Get Authorized WSS URL
```
GET https://api.upstox.com/v2/feed/market-data-feed/authorize
Headers: Authorization: Bearer {access_token}
         Accept: application/json

Response:
{
  "status": "success",
  "data": {
    "authorized_redirect_uri": "wss://xyz.upstox.com/...?code=<one-time-token>"
  }
}
```
> URL is one-time use. Reconnect = re-authorize.

### Step 2 — Connect WebSocket
```
wss://<authorized_redirect_uri>
```
Requires `followRedirects` support. Python `websockets` handles this natively.

### Step 3 — Subscribe
```json
{
  "guid": "any-unique-id",
  "method": "sub",
  "data": {
    "mode": "full",
    "instrumentKeys": ["NSE_INDEX|Nifty 50", "NSE_INDEX|Nifty Bank"]
  }
}
```

**Methods:** `sub` | `change_mode` | `unsub`  
**Modes:** `ltpc` | `full` | `option_chain`  
**Limit:** Max 100 instrumentKeys per socket

### Step 4 — Decode Response (Protobuf)
All messages are protobuf-encoded. Decode using `MarketDataFeed.proto`.

**FeedResponse structure:**
```
FeedResponse {
  type:      initial_feed | live_feed
  feeds:     map<instrumentKey, Feed>
  currentTs: int64 (timestamp)
}
```

---

## Protobuf Schema
**Proto file:** `https://assets.upstox.com/feed/market-data-feed/v1.1/MarketDataFeed.proto`

```protobuf
syntax = "proto3";
package com.upstox.marketdatafeeder.rpc.proto;

message LTPC {
  double ltp = 1;   // last traded price
  int64  ltt = 2;   // last traded time
  int64  ltq = 3;   // last traded quantity
  double cp  = 4;   // close price
}

message Quote {
  int32  bq  = 1;   // bid quantity
  double bp  = 2;   // bid price
  int32  bno = 3;   // bid number of orders
  int32  aq  = 4;   // ask quantity
  double ap  = 5;   // ask price
  int32  ano = 6;   // ask number of orders
  int64  bidQ = 7;
  int64  askQ = 8;
}

message MarketLevel {
  repeated Quote bidAskQuote = 1;  // 5-level depth
  int64 lut = 2;                   // last update time
}

message OptionGreeks {
  double op    = 1;  // option price
  double up    = 2;  // underlying price
  double iv    = 3;  // implied volatility
  double delta = 4;
  double theta = 5;
  double gamma = 6;
  double vega  = 7;
  double rho   = 8;
}

message ExtendedFeedDetails {
  double atp      = 1;   // avg traded price
  double cp       = 2;   // close price
  int64  vtt      = 3;   // volume traded today
  double oi       = 4;   // open interest
  double changeOi = 5;   // OI change
  double lastClose = 6;
  double tbq      = 7;   // total buy quantity  → ORDER FLOW
  double tsq      = 8;   // total sell quantity → ORDER FLOW
  double close    = 9;
  double lc       = 10;  // lower circuit
  double uc       = 11;  // upper circuit
  double yh       = 12;  // yearly high
  double yl       = 13;  // yearly low
  double fp       = 14;  // fill price
  int32  fv       = 15;  // fill volume
  int64  mbpBuy   = 16;  // market by price buy  → ORDER FLOW
  int64  mbpSell  = 17;  // market by price sell → ORDER FLOW
  int64  tv       = 18;  // traded volume
  double dhoi     = 19;  // day high open interest
  double dloi     = 20;  // day low open interest
  double sp       = 21;  // spot price
  double poi      = 22;  // previous open interest
}

message OHLC {
  string interval = 1;  // "1d", "I1", "I30"
  double open     = 2;
  double high     = 3;
  double low      = 4;
  double close    = 5;
  int32  volume   = 6;
  int64  ts       = 7;
  int64  vol      = 9;
}

message MarketFullFeed {
  LTPC                ltpc         = 1;
  MarketLevel         marketLevel  = 2;  // 5-level depth
  OptionGreeks        optionGreeks = 3;
  MarketOHLC          marketOHLC   = 4;  // OHLC intervals
  ExtendedFeedDetails eFeedDetails = 5;
}

message IndexFullFeed {
  LTPC       ltpc      = 1;
  MarketOHLC marketOHLC = 2;
  double     lastClose = 3;
  double     yh        = 4;
  double     yl        = 5;
}

message OptionChain {
  LTPC                ltpc         = 1;
  Quote               bidAskQuote  = 2;
  OptionGreeks        optionGreeks = 3;
  ExtendedFeedDetails eFeedDetails = 4;
}

message Feed {
  oneof FeedUnion {
    LTPC      ltpc = 1;
    FullFeed  ff   = 2;
    OptionChain oc = 3;
  }
}

message FeedResponse {
  Type                type      = 1;
  map<string, Feed>   feeds     = 2;
  int64               currentTs = 3;
}
```

---

## Key Fields for Our Analysis

| Analysis | Fields |
|---|---|
| Order Flow Delta | `tbq - tsq` (ExtendedFeedDetails) |
| Order Flow Bias | `mbpBuy` vs `mbpSell` |
| OI Analysis | `oi`, `changeOi`, `dhoi`, `dloi`, `poi` |
| Greeks | `delta`, `gamma`, `theta`, `vega`, `iv` (OptionGreeks) |
| Market Depth | `bidAskQuote[0..4]` (MarketLevel) |
| OHLC / SMA input | `marketOHLC.ohlc[]` intervals |
| Volume | `vtt`, `tv` |

---

## Positions API
```
GET https://api.upstox.com/v2/portfolio/short-term-positions
Headers: Authorization: Bearer {access_token}

Response fields per position:
  exchange, instrument_token, trading_symbol,
  quantity, average_price, last_price,
  pnl, unrealised, realised,
  buy_price, sell_price, close_price,
  product ("I"=Intraday, "D"=Delivery)
```

---

## Place Order API
```
POST https://api-hft.upstox.com/v2/order/place
Content-Type: application/json
Authorization: Bearer {access_token}

Body:
{
  "quantity":          int,        // required
  "product":           "I"|"D"|"MTF",
  "validity":          "DAY"|"IOC",
  "price":             float,
  "instrument_token":  string,     // e.g. "NSE_FO|35001"
  "order_type":        "MARKET"|"LIMIT"|"SL"|"SL-M",
  "transaction_type":  "BUY"|"SELL",
  "disclosed_quantity": int,
  "trigger_price":     float,
  "is_amo":            bool,       // after market order
  "tag":               string      // optional custom tag
}

Response: { "status": "success", "data": { "order_id": "..." } }
```

---

## Instrument Keys (Common)
```
NSE_INDEX|Nifty 50
NSE_INDEX|Nifty Bank
NSE_INDEX|Nifty Fin Service
NSE_FO|<token>       # futures/options (lookup via instruments CSV)
```

Instruments CSV: `https://assets.upstox.com/market-quote/instruments/exchange/NSE.csv`

---

## Rate Limits
- Market data: no hard limit on WSS ticks
- REST API: standard rate limits apply per endpoint
- Max 100 instrument subscriptions per WSS connection
