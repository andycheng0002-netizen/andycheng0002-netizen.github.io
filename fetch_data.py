"""每天從證交所抓「全部上市股票收盤價」和「三大法人買賣超」，整理成 app 讀的 JSON。

由 GitHub Actions 自動執行，不需要自己跑。
  python3 fetch_data.py               抓最近幾天缺少的資料
  python3 fetch_data.py --backfill 100  補抓到有 100 個交易日（第一次用）
"""
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
RAW = os.path.join(DATA, "raw")
STK = os.path.join(DATA, "stocks")
KEEP_DAYS = 130          # 保留幾個交易日
PAUSE = 3.5              # 每次查詢間隔秒數，避免被證交所擋
TPE = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (personal stock app; GitHub Actions)"
CODE_RE = re.compile(r"^(\d{4}|00\d{2,4}[A-Z]?)$")

PRICE_URL = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={d}&type=ALLBUT0999&response=json"
INST_URL = "https://www.twse.com.tw/rwd/zh/fund/T86?date={d}&selectType=ALLBUT0999&response=json"


def get_json(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # 網路錯誤就等一下再試
            print(f"  第 {attempt + 1} 次失敗：{e}", flush=True)
            time.sleep(PAUSE * (attempt + 2))
    return None


def num(s):
    s = str(s).replace(",", "").replace("+", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


def clean(s):
    return re.sub(r"<[^>]+>", "", str(s)).replace("=", "").replace('"', "").strip()


def tables(j):
    """MI_INDEX 有新舊兩種格式，都轉成 (fields, data) 清單。"""
    out = []
    for t in j.get("tables") or []:
        if t.get("fields") and t.get("data"):
            out.append((t["fields"], t["data"]))
    for k, v in j.items():
        if k.startswith("fields") and isinstance(v, list):
            d = j.get("data" + k[6:])
            if d:
                out.append((v, d))
    return out


def parse_prices(j):
    res = {}
    for fields, data in tables(j):
        f = [clean(x) for x in fields]
        if "證券代號" not in f or "收盤價" not in f:
            continue
        ic, inm, ip = f.index("證券代號"), f.index("證券名稱"), f.index("收盤價")
        for row in data:
            code = clean(row[ic])
            close = num(clean(row[ip]))
            if CODE_RE.match(code) and close and close > 0:
                res[code] = [close, clean(row[inm])]
    return res


def parse_inst(j):
    f = [clean(x) for x in j.get("fields") or []]
    if "證券代號" not in f:
        return {}

    def idx(test):
        for i, x in enumerate(f):
            if test(x):
                return i
        return -1

    ic = f.index("證券代號")
    i_f1 = idx(lambda x: "外陸資買賣超" in x or ("外資" in x and "買賣超" in x and "自營商" not in x))
    i_f2 = idx(lambda x: "外資自營商買賣超" in x)
    i_t = idx(lambda x: "投信買賣超" in x)
    i_d = idx(lambda x: x.startswith("自營商買賣超"))
    i_tot = idx(lambda x: "三大法人買賣超" in x)
    res = {}
    for row in j.get("data") or []:
        code = clean(row[ic])
        if not CODE_RE.match(code):
            continue

        def v(i):
            return int(num(clean(row[i])) or 0) if i >= 0 else 0

        fo = v(i_f1) + v(i_f2)
        res[code] = [fo, v(i_t), v(i_d), v(i_tot)]
    return res


def raw_path(day):
    return os.path.join(RAW, day.isoformat() + ".json")


def load_raw(day):
    p = raw_path(day)
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    return None


def fetch_day(day):
    """回傳 True 表示這天是交易日且已存檔。"""
    old = load_raw(day)
    if old and old.get("i"):
        return True
    d = day.strftime("%Y%m%d")
    print(f"抓取 {day}", flush=True)
    prices = old["p"] if old else None
    if not prices:
        pj = get_json(PRICE_URL.format(d=d))
        time.sleep(PAUSE)
        if not pj or pj.get("stat") != "OK":
            print("  沒有收盤資料（假日或尚未公布）", flush=True)
            return False
        prices = parse_prices(pj)
        if not prices:
            print("  收盤資料格式看不懂，略過", flush=True)
            return False
    ij = get_json(INST_URL.format(d=d))
    time.sleep(PAUSE)
    inst = parse_inst(ij) if ij and ij.get("stat") == "OK" else {}
    if not inst:
        print("  三大法人尚未公布，先存收盤價", flush=True)
    os.makedirs(RAW, exist_ok=True)
    with open(raw_path(day), "w", encoding="utf-8") as fh:
        json.dump({"date": day.isoformat(), "p": prices, "i": inst}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"  完成：{len(prices)} 檔收盤價，{len(inst)} 檔法人", flush=True)
    return True


def rebuild():
    files = sorted(f for f in os.listdir(RAW) if f.endswith(".json")) if os.path.isdir(RAW) else []
    for old in files[:-KEEP_DAYS]:
        os.remove(os.path.join(RAW, old))
    files = files[-KEEP_DAYS:]
    stocks = {}
    for fn in files:
        with open(os.path.join(RAW, fn), encoding="utf-8") as fh:
            day = json.load(fh)
        d = day["date"]
        for code, (close, name) in day["p"].items():
            s = stocks.setdefault(code, {"code": code, "name": name, "p": [], "i": []})
            s["name"] = name
            s["p"].append([d, close])
        for code, vals in day.get("i", {}).items():
            s = stocks.setdefault(code, {"code": code, "name": code, "p": [], "i": []})
            s["i"].append([d] + vals)
    os.makedirs(STK, exist_ok=True)
    for fn in os.listdir(STK):
        if fn[:-5] not in stocks:
            os.remove(os.path.join(STK, fn))
    for code, s in stocks.items():
        with open(os.path.join(STK, code + ".json"), "w", encoding="utf-8") as fh:
            json.dump(s, fh, ensure_ascii=False, separators=(",", ":"))
    index = {
        "updated": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M"),
        "first_date": files[0][:10] if files else None,
        "last_date": files[-1][:10] if files else None,
        "days": len(files),
        "stocks": sorted([c, s["name"]] for c, s in stocks.items()),
    }
    with open(os.path.join(DATA, "index.json"), "w", encoding="utf-8") as fh:
        json.dump(index, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"整理完成：{len(stocks)} 檔股票，{len(files)} 個交易日", flush=True)


def main():
    backfill = 0
    if "--backfill" in sys.argv:
        try:
            backfill = int(sys.argv[sys.argv.index("--backfill") + 1] or 0)
        except (IndexError, ValueError):
            backfill = 0
    today = dt.datetime.now(TPE).date()
    if backfill > 0:
        have, day, limit = 0, today, today - dt.timedelta(days=int(backfill * 1.6) + 20)
        while have < backfill and day >= limit:
            if day.weekday() < 5 and fetch_day(day):
                have += 1
            day -= dt.timedelta(days=1)
    else:
        for back in range(7):
            day = today - dt.timedelta(days=back)
            if day.weekday() < 5:
                fetch_day(day)
    rebuild()


if __name__ == "__main__":
    main()
