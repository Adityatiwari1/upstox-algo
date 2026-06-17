from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Type(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    initial_feed: _ClassVar[Type]
    live_feed: _ClassVar[Type]
initial_feed: Type
live_feed: Type

class LTPC(_message.Message):
    __slots__ = ("ltp", "ltt", "ltq", "cp")
    LTP_FIELD_NUMBER: _ClassVar[int]
    LTT_FIELD_NUMBER: _ClassVar[int]
    LTQ_FIELD_NUMBER: _ClassVar[int]
    CP_FIELD_NUMBER: _ClassVar[int]
    ltp: float
    ltt: int
    ltq: int
    cp: float
    def __init__(self, ltp: _Optional[float] = ..., ltt: _Optional[int] = ..., ltq: _Optional[int] = ..., cp: _Optional[float] = ...) -> None: ...

class MarketLevel(_message.Message):
    __slots__ = ("bidAskQuote", "lut")
    BIDASKQUOTE_FIELD_NUMBER: _ClassVar[int]
    LUT_FIELD_NUMBER: _ClassVar[int]
    bidAskQuote: _containers.RepeatedCompositeFieldContainer[Quote]
    lut: int
    def __init__(self, bidAskQuote: _Optional[_Iterable[_Union[Quote, _Mapping]]] = ..., lut: _Optional[int] = ...) -> None: ...

class MarketOHLC(_message.Message):
    __slots__ = ("ohlc",)
    OHLC_FIELD_NUMBER: _ClassVar[int]
    ohlc: _containers.RepeatedCompositeFieldContainer[OHLC]
    def __init__(self, ohlc: _Optional[_Iterable[_Union[OHLC, _Mapping]]] = ...) -> None: ...

class Quote(_message.Message):
    __slots__ = ("bq", "bp", "bno", "aq", "ap", "ano", "bidQ", "askQ")
    BQ_FIELD_NUMBER: _ClassVar[int]
    BP_FIELD_NUMBER: _ClassVar[int]
    BNO_FIELD_NUMBER: _ClassVar[int]
    AQ_FIELD_NUMBER: _ClassVar[int]
    AP_FIELD_NUMBER: _ClassVar[int]
    ANO_FIELD_NUMBER: _ClassVar[int]
    BIDQ_FIELD_NUMBER: _ClassVar[int]
    ASKQ_FIELD_NUMBER: _ClassVar[int]
    bq: int
    bp: float
    bno: int
    aq: int
    ap: float
    ano: int
    bidQ: int
    askQ: int
    def __init__(self, bq: _Optional[int] = ..., bp: _Optional[float] = ..., bno: _Optional[int] = ..., aq: _Optional[int] = ..., ap: _Optional[float] = ..., ano: _Optional[int] = ..., bidQ: _Optional[int] = ..., askQ: _Optional[int] = ...) -> None: ...

class OptionGreeks(_message.Message):
    __slots__ = ("op", "up", "iv", "delta", "theta", "gamma", "vega", "rho")
    OP_FIELD_NUMBER: _ClassVar[int]
    UP_FIELD_NUMBER: _ClassVar[int]
    IV_FIELD_NUMBER: _ClassVar[int]
    DELTA_FIELD_NUMBER: _ClassVar[int]
    THETA_FIELD_NUMBER: _ClassVar[int]
    GAMMA_FIELD_NUMBER: _ClassVar[int]
    VEGA_FIELD_NUMBER: _ClassVar[int]
    RHO_FIELD_NUMBER: _ClassVar[int]
    op: float
    up: float
    iv: float
    delta: float
    theta: float
    gamma: float
    vega: float
    rho: float
    def __init__(self, op: _Optional[float] = ..., up: _Optional[float] = ..., iv: _Optional[float] = ..., delta: _Optional[float] = ..., theta: _Optional[float] = ..., gamma: _Optional[float] = ..., vega: _Optional[float] = ..., rho: _Optional[float] = ...) -> None: ...

class ExtendedFeedDetails(_message.Message):
    __slots__ = ("atp", "cp", "vtt", "oi", "changeOi", "lastClose", "tbq", "tsq", "close", "lc", "uc", "yh", "yl", "fp", "fv", "mbpBuy", "mbpSell", "tv", "dhoi", "dloi", "sp", "poi")
    ATP_FIELD_NUMBER: _ClassVar[int]
    CP_FIELD_NUMBER: _ClassVar[int]
    VTT_FIELD_NUMBER: _ClassVar[int]
    OI_FIELD_NUMBER: _ClassVar[int]
    CHANGEOI_FIELD_NUMBER: _ClassVar[int]
    LASTCLOSE_FIELD_NUMBER: _ClassVar[int]
    TBQ_FIELD_NUMBER: _ClassVar[int]
    TSQ_FIELD_NUMBER: _ClassVar[int]
    CLOSE_FIELD_NUMBER: _ClassVar[int]
    LC_FIELD_NUMBER: _ClassVar[int]
    UC_FIELD_NUMBER: _ClassVar[int]
    YH_FIELD_NUMBER: _ClassVar[int]
    YL_FIELD_NUMBER: _ClassVar[int]
    FP_FIELD_NUMBER: _ClassVar[int]
    FV_FIELD_NUMBER: _ClassVar[int]
    MBPBUY_FIELD_NUMBER: _ClassVar[int]
    MBPSELL_FIELD_NUMBER: _ClassVar[int]
    TV_FIELD_NUMBER: _ClassVar[int]
    DHOI_FIELD_NUMBER: _ClassVar[int]
    DLOI_FIELD_NUMBER: _ClassVar[int]
    SP_FIELD_NUMBER: _ClassVar[int]
    POI_FIELD_NUMBER: _ClassVar[int]
    atp: float
    cp: float
    vtt: int
    oi: float
    changeOi: float
    lastClose: float
    tbq: float
    tsq: float
    close: float
    lc: float
    uc: float
    yh: float
    yl: float
    fp: float
    fv: int
    mbpBuy: int
    mbpSell: int
    tv: int
    dhoi: float
    dloi: float
    sp: float
    poi: float
    def __init__(self, atp: _Optional[float] = ..., cp: _Optional[float] = ..., vtt: _Optional[int] = ..., oi: _Optional[float] = ..., changeOi: _Optional[float] = ..., lastClose: _Optional[float] = ..., tbq: _Optional[float] = ..., tsq: _Optional[float] = ..., close: _Optional[float] = ..., lc: _Optional[float] = ..., uc: _Optional[float] = ..., yh: _Optional[float] = ..., yl: _Optional[float] = ..., fp: _Optional[float] = ..., fv: _Optional[int] = ..., mbpBuy: _Optional[int] = ..., mbpSell: _Optional[int] = ..., tv: _Optional[int] = ..., dhoi: _Optional[float] = ..., dloi: _Optional[float] = ..., sp: _Optional[float] = ..., poi: _Optional[float] = ...) -> None: ...

class OHLC(_message.Message):
    __slots__ = ("interval", "open", "high", "low", "close", "volume", "ts", "vol")
    INTERVAL_FIELD_NUMBER: _ClassVar[int]
    OPEN_FIELD_NUMBER: _ClassVar[int]
    HIGH_FIELD_NUMBER: _ClassVar[int]
    LOW_FIELD_NUMBER: _ClassVar[int]
    CLOSE_FIELD_NUMBER: _ClassVar[int]
    VOLUME_FIELD_NUMBER: _ClassVar[int]
    TS_FIELD_NUMBER: _ClassVar[int]
    VOL_FIELD_NUMBER: _ClassVar[int]
    interval: str
    open: float
    high: float
    low: float
    close: float
    volume: int
    ts: int
    vol: int
    def __init__(self, interval: _Optional[str] = ..., open: _Optional[float] = ..., high: _Optional[float] = ..., low: _Optional[float] = ..., close: _Optional[float] = ..., volume: _Optional[int] = ..., ts: _Optional[int] = ..., vol: _Optional[int] = ...) -> None: ...

class MarketFullFeed(_message.Message):
    __slots__ = ("ltpc", "marketLevel", "optionGreeks", "marketOHLC", "eFeedDetails")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    MARKETLEVEL_FIELD_NUMBER: _ClassVar[int]
    OPTIONGREEKS_FIELD_NUMBER: _ClassVar[int]
    MARKETOHLC_FIELD_NUMBER: _ClassVar[int]
    EFEEDDETAILS_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    marketLevel: MarketLevel
    optionGreeks: OptionGreeks
    marketOHLC: MarketOHLC
    eFeedDetails: ExtendedFeedDetails
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., marketLevel: _Optional[_Union[MarketLevel, _Mapping]] = ..., optionGreeks: _Optional[_Union[OptionGreeks, _Mapping]] = ..., marketOHLC: _Optional[_Union[MarketOHLC, _Mapping]] = ..., eFeedDetails: _Optional[_Union[ExtendedFeedDetails, _Mapping]] = ...) -> None: ...

class IndexFullFeed(_message.Message):
    __slots__ = ("ltpc", "marketOHLC", "lastClose", "yh", "yl")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    MARKETOHLC_FIELD_NUMBER: _ClassVar[int]
    LASTCLOSE_FIELD_NUMBER: _ClassVar[int]
    YH_FIELD_NUMBER: _ClassVar[int]
    YL_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    marketOHLC: MarketOHLC
    lastClose: float
    yh: float
    yl: float
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., marketOHLC: _Optional[_Union[MarketOHLC, _Mapping]] = ..., lastClose: _Optional[float] = ..., yh: _Optional[float] = ..., yl: _Optional[float] = ...) -> None: ...

class FullFeed(_message.Message):
    __slots__ = ("marketFF", "indexFF")
    MARKETFF_FIELD_NUMBER: _ClassVar[int]
    INDEXFF_FIELD_NUMBER: _ClassVar[int]
    marketFF: MarketFullFeed
    indexFF: IndexFullFeed
    def __init__(self, marketFF: _Optional[_Union[MarketFullFeed, _Mapping]] = ..., indexFF: _Optional[_Union[IndexFullFeed, _Mapping]] = ...) -> None: ...

class OptionChain(_message.Message):
    __slots__ = ("ltpc", "bidAskQuote", "optionGreeks", "eFeedDetails")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    BIDASKQUOTE_FIELD_NUMBER: _ClassVar[int]
    OPTIONGREEKS_FIELD_NUMBER: _ClassVar[int]
    EFEEDDETAILS_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    bidAskQuote: Quote
    optionGreeks: OptionGreeks
    eFeedDetails: ExtendedFeedDetails
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., bidAskQuote: _Optional[_Union[Quote, _Mapping]] = ..., optionGreeks: _Optional[_Union[OptionGreeks, _Mapping]] = ..., eFeedDetails: _Optional[_Union[ExtendedFeedDetails, _Mapping]] = ...) -> None: ...

class Feed(_message.Message):
    __slots__ = ("ltpc", "ff", "oc")
    LTPC_FIELD_NUMBER: _ClassVar[int]
    FF_FIELD_NUMBER: _ClassVar[int]
    OC_FIELD_NUMBER: _ClassVar[int]
    ltpc: LTPC
    ff: FullFeed
    oc: OptionChain
    def __init__(self, ltpc: _Optional[_Union[LTPC, _Mapping]] = ..., ff: _Optional[_Union[FullFeed, _Mapping]] = ..., oc: _Optional[_Union[OptionChain, _Mapping]] = ...) -> None: ...

class FeedResponse(_message.Message):
    __slots__ = ("type", "feeds", "currentTs")
    class FeedsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: Feed
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[Feed, _Mapping]] = ...) -> None: ...
    TYPE_FIELD_NUMBER: _ClassVar[int]
    FEEDS_FIELD_NUMBER: _ClassVar[int]
    CURRENTTS_FIELD_NUMBER: _ClassVar[int]
    type: Type
    feeds: _containers.MessageMap[str, Feed]
    currentTs: int
    def __init__(self, type: _Optional[_Union[Type, str]] = ..., feeds: _Optional[_Mapping[str, Feed]] = ..., currentTs: _Optional[int] = ...) -> None: ...
