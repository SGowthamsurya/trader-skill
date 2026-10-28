"""Synthetic bhavcopies for the india tests. Every code, name, ISIN and price is made up."""

import datetime
import io
import os
import sys
import zipfile

from _load import ROOT

INDIA = os.path.join(ROOT, "india")
if INDIA not in sys.path:
    sys.path.insert(0, INDIA)

BSE_HEAD = ("SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,NO_TRADES,NO_OF_SHRS,"
            "NET_TURNOV,TDCLOINDI,ISIN_CODE,TRADING_DATE,FILLER2,FILLER3")
NSE_HEAD = "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN,"
UDIFF_HEAD = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,"
              "StrkPric,OptnTp,FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,"
              "SttlmPric,OpnIntrst,ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,"
              "Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def d(s):
    return datetime.date.fromisoformat(s)


def bse_legacy(day, rows):
    """rows: (code, name, isin, close, prev, shares, value)"""
    t = d(day).strftime("%d-%b-%y")
    lines = [BSE_HEAD] + ["%s,%-12s,B ,Q,%s,%s,%s,%s,%s,%s,10,%s,%s,,%s,%s,," % (
        c, n, cl, cl, cl, cl, cl, pv, sh, val, isin, t) for c, n, isin, cl, pv, sh, val in rows]
    return zip_of("EQ_ISINCODE_%s.CSV" % d(day).strftime("%d%m%y"), "\n".join(lines) + "\n")


def nse_legacy(day, rows):
    """rows: (symbol, series, isin, close, prev, qty, value)"""
    t = d(day).strftime("%d-%b-%Y").upper()
    lines = [NSE_HEAD] + ["%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,5,%s," % (
        s, se, cl, cl, cl, cl, cl, pv, q, v, t, isin) for s, se, isin, cl, pv, q, v in rows]
    return zip_of("cm%sbhav.csv" % d(day).strftime("%d%b%Y").upper(), "\n".join(lines) + "\n")


def udiff(day, src, rows):
    """rows: (id, symbol, series, isin, name, close, prev, qty, value)"""
    lines = [UDIFF_HEAD] + ["%s,%s,CM,%s,STK,%s,%s,%s,%s,,,,,%s,%s,%s,%s,%s,%s,%s,,%s,,,%s,%s,9,F1,1,,,,," % (
        day, day, src, i, isin, s, se, n, cl, cl, cl, cl, cl, pv, cl, q, v) for i, s, se, isin, n, cl, pv, q, v in rows]
    return ("\n".join(lines) + "\n").encode()


def zip_of(name, text, extra=None):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(name, text)
        if extra:
            z.writestr(extra, text)
    return buf.getvalue()


def put_file(cache, source, day, data):
    return cache.put(data, {"kind": "fetch", "ok": True, "source": source, "url": "https://x/%s/%s" % (source, day),
                            "date": day, "retrieved_at": "2026-10-28T00:00:00Z", "expect": "zip"})


def put_refusal(cache, source, day, reason="not-data"):
    return cache.note({"kind": "fetch", "ok": False, "source": source, "url": "https://x/%s/%s" % (source, day),
                       "date": day, "reason": reason, "detail": "synthetic", "retrieved_at": "2026-10-28T00:00:00Z"})


def weekdays(a, b):
    x = d(a)
    while x <= d(b):
        if x.weekday() < 5:
            yield x.isoformat()
        x += datetime.timedelta(days=1)
