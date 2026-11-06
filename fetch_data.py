"""每天從證交所抓資料，整理成 app 讀的 JSON。

抓的內容：全部上市股票收盤價、三大法人買賣超、融資餘額（算大盤融資維持率）、
加權指數歷史（2000 年起，大跌比較用）、盤中加權指數（估算當天維持率）。

由 GitHub Actions 自動執行，不需要自己跑。
  python3 fetch_data.py                 抓最近幾天缺少的資料
  python3 fetch_data.py --backfill 100  補抓到有 100 個交易日（第一次用）
  python3 fetch_data.py --intraday      盤中：只抓即時加權指數，估算今日維持率
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
MARGIN_URL = "https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?date={d}&selectType=ALL&response=json"
TAIEX_URL = "https://www.twse.com.tw/rwd/zh/TAIEX/MI_5MINS_HIST?date={d}&response=json"
MIS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_t00.tw&json=1&delay=0"
MIS_STOCKS_URL = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={ch}&json=1&delay=0"
SHARES_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TAIFEX_INST_URL = "https://www.taifex.com.tw/cht/3/futContractsDateDown"
TAIFEX_LARGE_URL = "https://www.taifex.com.tw/cht/3/largeTraderFutDown"
TAIFEX_VIX_URL = "https://www.taifex.com.tw/cht/7/vixDaily3MNew"
TAIFEX_OPT_URL = "https://www.taifex.com.tw/cht/3/callsAndPutsDateDown"
TAIFEX_OPT_LARGE_URL = "https://www.taifex.com.tw/cht/3/largeTraderOptDown"
STOCK_RE = re.compile(r"^[1-9]\d{3}$")   # 指數成分：一般股票，不含 ETF
HIST_START = (2000, 1)


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
        isg = f.index("漲跌(+/-)") if "漲跌(+/-)" in f else -1
        idf = f.index("漲跌價差") if "漲跌價差" in f else -1
        for row in data:
            code = clean(row[ic])
            close = num(clean(row[ip]))
            if CODE_RE.match(code) and close and close > 0:
                chg = None
                if isg >= 0 and idf >= 0:
                    sg, df = clean(row[isg]), num(clean(row[idf]))
                    if df is not None and sg in ("+", "-", ""):
                        chg = -df if sg == "-" else df
                res[code] = [close, clean(row[inm]), chg]
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


def roc_to_iso(s):
    m = re.match(r"^(\d{2,4})/(\d{1,2})/(\d{1,2})", clean(s))
    if not m:
        return None
    y = int(m.group(1))
    if y < 1000:
        y += 1911
    return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"


def parse_margin(j, prices):
    """大盤融資維持率 = 所有融資股票（餘額張數 × 1000 × 收盤價）÷ 融資金額。"""
    amt, val, n = None, 0.0, 0
    for fields, data in tables(j):
        f = [clean(x) for x in fields]
        if "項目" in f and "今日餘額" in f:
            ii, it = f.index("項目"), f.index("今日餘額")
            for row in data:
                if "融資金額" in clean(row[ii]):
                    amt = num(clean(row[it]))
        else:
            ic = f.index("代號") if "代號" in f else (f.index("股票代號") if "股票代號" in f else -1)
            if ic < 0 or "今日餘額" not in f:
                continue
            it = f.index("今日餘額")  # 第一個「今日餘額」是融資
            for row in data:
                code, bal = clean(row[ic]), num(clean(row[it]))
                if bal and code in prices:
                    val += bal * 1000 * prices[code][0]
                    n += 1
    if not amt or not val:
        return None
    return {"r": round(val / (amt * 1000) * 100, 2), "amt": amt, "n": n}


def fetch_day(day):
    """回傳 True 表示這天是交易日且已存檔。"""
    old = load_raw(day) or {}
    if old.get("i") and old.get("m"):
        return True
    d = day.strftime("%Y%m%d")
    print(f"抓取 {day}", flush=True)
    prices = old.get("p")
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
    inst = old.get("i") or {}
    if not inst:
        ij = get_json(INST_URL.format(d=d))
        time.sleep(PAUSE)
        inst = parse_inst(ij) if ij and ij.get("stat") == "OK" else {}
        if not inst:
            print("  三大法人尚未公布", flush=True)
    margin = old.get("m")
    if not margin:
        mj = get_json(MARGIN_URL.format(d=d))
        time.sleep(PAUSE)
        margin = parse_margin(mj, prices) if mj and mj.get("stat") == "OK" else None
        if not margin:
            print("  融資資料尚未公布", flush=True)
    os.makedirs(RAW, exist_ok=True)
    with open(raw_path(day), "w", encoding="utf-8") as fh:
        json.dump({"date": day.isoformat(), "p": prices, "i": inst, "m": margin}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"  完成：{len(prices)} 檔收盤價，{len(inst)} 檔法人，維持率 {margin['r'] if margin else '—'}", flush=True)
    return True


def update_taiex(today):
    """加權指數每日開高低收，2000 年起。缺的月份補抓，最近兩個月每次重抓。"""
    path = os.path.join(DATA, "taiex.json")
    rows = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            for r in json.load(fh)["rows"]:
                rows[r[0]] = r
    have = {d[:7] for d in rows}
    y, m = HIST_START
    months = []
    while (y, m) <= (today.year, today.month):
        months.append((y, m))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    recent = set(months[-2:])
    todo = [ym for ym in months if f"{ym[0]:04d}-{ym[1]:02d}" not in have or ym in recent]
    print(f"加權指數歷史：要抓 {len(todo)} 個月", flush=True)
    for y, m in todo:
        j = get_json(TAIEX_URL.format(d=f"{y:04d}{m:02d}01"))
        time.sleep(PAUSE)
        if not j or j.get("stat") != "OK":
            continue
        f = [clean(x) for x in j.get("fields") or []]
        for fields, data in ([(f, j.get("data") or [])] if f else tables(j)):
            f2 = [clean(x) for x in fields]
            if "日期" not in f2 or "收盤指數" not in f2:
                continue
            idx = [f2.index("日期")] + [next((i for i, x in enumerate(f2) if x.startswith(k)), -1) for k in ("開盤", "最高", "最低", "收盤")]
            for row in data:
                iso = roc_to_iso(row[idx[0]])
                vals = [num(clean(row[i])) if i >= 0 else None for i in idx[1:]]
                if iso and vals[3]:
                    rows[iso] = [iso] + [v if v is not None else vals[3] for v in vals]
    out = sorted(rows.values())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"updated": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M"), "rows": out}, fh, separators=(",", ":"))
    print(f"加權指數歷史：共 {len(out)} 天", flush=True)
    return {r[0]: r[4] for r in out}


def build_market(closes):
    """融資維持率歷史＋維持率對指數的彈性（估算盤中用）。"""
    files = sorted(f for f in os.listdir(RAW) if f.endswith(".json")) if os.path.isdir(RAW) else []
    series = []
    for fn in files:
        with open(os.path.join(RAW, fn), encoding="utf-8") as fh:
            day = json.load(fh)
        m = day.get("m")
        if m and day["date"] in closes:
            series.append([day["date"], m["r"], m["amt"], closes[day["date"]]])
    sxy = sxx = 0.0
    pairs = []
    for a, b in zip(series, series[1:]):
        x, yv = b[3] / a[3] - 1, b[1] / a[1] - 1
        pairs.append((x, yv))
        sxy += x * yv
        sxx += x * x
    e = sxy / sxx if sxx > 0 and len(pairs) >= 10 else 1.6
    e = max(0.8, min(3.0, e))
    res = [yv - e * x for x, yv in pairs]
    sigma = (sum(r * r for r in res) / len(res)) ** 0.5 if len(res) >= 10 else 0.008
    out = {"updated": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M"), "elasticity": round(e, 3),
           "sigma": round(sigma, 5), "margin": series}
    with open(os.path.join(DATA, "market.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"融資維持率：{len(series)} 天，彈性 {e:.2f}", flush=True)


def intraday():
    """盤中抓即時加權指數，用彈性估算今天的融資維持率。"""
    j = get_json(MIS_URL)
    try:
        q = j["msgArray"][0]
        z = num(q.get("z")) or num(q.get("o"))
        prev = num(q.get("y"))
        d = q.get("d")
        t = q.get("t")
    except Exception:
        print("讀不到即時指數", flush=True)
        return
    if not z or not prev:
        print("即時指數尚未開盤", flush=True)
        return
    iso = f"{d[:4]}-{d[4:6]}-{d[6:8]}"
    out = {"date": iso, "time": t, "index": z, "prev": prev, "updated": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M")}
    with open(os.path.join(DATA, "intraday.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh, separators=(",", ":"))
    print(f"盤中指數 {iso} {t}：{z}（昨收 {prev}）", flush=True)
    time.sleep(1)
    contrib_live(z, prev, t, iso)


def get_raw(url, data=None):
    """抓網頁或 CSV（期交所用 POST，回傳 Big5 文字）。"""
    import urllib.parse
    body = urllib.parse.urlencode(data).encode() if data else None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=body, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=40) as r:
                b = r.read()
            for enc in ("utf-8", "cp950", "big5"):
                try:
                    return b.decode(enc)
                except UnicodeDecodeError:
                    pass
            return b.decode("cp950", errors="replace")
        except Exception as e:
            print(f"  第 {attempt + 1} 次失敗：{e}", flush=True)
            time.sleep(PAUSE * (attempt + 2))
    return None


def update_shares():
    """上市公司已發行股數（算貢獻點數用），每週更新一次。"""
    path = os.path.join(DATA, "shares.json")
    old = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        if old.get("date", "") >= (dt.datetime.now(TPE).date() - dt.timedelta(days=7)).isoformat() and old.get("shares"):
            return old["shares"]
    j = get_json(SHARES_URL)
    shares = {}
    if isinstance(j, list):
        for row in j:
            code = clean(row.get("公司代號", ""))
            key = next((k for k in row if "已發行普通股數" in k), None)
            n = num(row.get(key, "")) if key else None
            if code and n:
                shares[code] = n
    if not shares:
        print("讀不到已發行股數，沿用舊資料", flush=True)
        return old.get("shares", {})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"date": dt.datetime.now(TPE).date().isoformat(), "shares": shares}, fh, separators=(",", ":"))
    print(f"已發行股數：{len(shares)} 家", flush=True)
    return shares


def build_contrib(closes, shares):
    """每檔股票對加權指數的貢獻點數 ≈ 昨日指數 × 市值變動 ÷ 昨日總市值。"""
    files = sorted(f for f in os.listdir(RAW) if f.endswith(".json")) if os.path.isdir(RAW) else []
    if len(files) < 2 or not shares:
        return
    with open(os.path.join(RAW, files[-1]), encoding="utf-8") as fh:
        today = json.load(fh)
    with open(os.path.join(RAW, files[-2]), encoding="utf-8") as fh:
        prev = json.load(fh)
    d, pd_ = today["date"], prev["date"]
    if d not in closes or pd_ not in closes:
        print("貢獻點數：缺少加權指數，略過", flush=True)
        return
    rows, total_prev = [], 0.0
    for code, v in today["p"].items():
        if not STOCK_RE.match(code) or code not in shares:
            continue
        close = v[0]
        chg = v[2] if len(v) > 2 and v[2] is not None else (close - prev["p"][code][0] if code in prev["p"] else None)
        if chg is None:
            continue
        ref = close - chg
        if ref <= 0:
            continue
        total_prev += shares[code] * ref
        rows.append([code, v[1], close, round(chg, 2), shares[code]])
    if not total_prev:
        return
    idx_prev = closes[pd_]
    out = []
    for code, name, close, chg, sh in rows:
        pts = idx_prev * sh * chg / total_prev
        out.append([code, name, close, chg, round(chg / (close - chg) * 100, 2), round(pts, 2), round(sh * close / 1e8)])
    out.sort(key=lambda r: -r[6])
    res = {"date": d, "index": closes[d], "prev": idx_prev, "total_prev": total_prev, "stocks": out}
    with open(os.path.join(DATA, "contrib.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"貢獻點數：{len(out)} 檔，指數 {idx_prev} → {closes[d]}", flush=True)


def contrib_live(index_now, index_prev, t, iso):
    """盤中：用即時股價算前 50 大權值股的貢獻點數。"""
    path = os.path.join(DATA, "contrib.json")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        base = json.load(fh)
    top = base["stocks"][:50]
    sh = {r[0]: r[6] * 1e8 / r[2] for r in top}   # 由市值反推股數
    total = sum(r[6] * 1e8 for r in base["stocks"])  # 昨日收盤總市值
    ch = "|".join(f"tse_{r[0]}.tw" for r in top)
    j = get_json(MIS_STOCKS_URL.format(ch=ch))
    out = []
    for q in (j or {}).get("msgArray", []):
        code, y = q.get("c"), num(q.get("y"))
        z = num(q.get("z"))
        if z is None:
            b = str(q.get("b", "")).split("_")[0]
            z = num(b) or y
        if not code or not y or code not in sh:
            continue
        chg = z - y
        out.append([code, q.get("n", code), z, round(chg, 2), round(chg / y * 100, 2), round(index_prev * sh[code] * chg / total, 2), round(sh[code] * z / 1e8)])
    if not out:
        print("讀不到即時個股", flush=True)
        return
    out.sort(key=lambda r: -r[6])
    with open(os.path.join(DATA, "contrib_live.json"), "w", encoding="utf-8") as fh:
        json.dump({"date": iso, "time": t, "index": index_now, "prev": index_prev, "stocks": out}, fh, ensure_ascii=False, separators=(",", ":"))
    print(f"盤中貢獻：{len(out)} 檔", flush=True)


def parse_csv_text(text):
    import csv, io
    return [[c.strip() for c in row] for row in csv.reader(io.StringIO(text)) if row]


def first_num(s):
    m = re.search(r"-?[\d,]+(?:\.\d+)?", str(s))
    return num(m.group(0)) if m else None


def update_futures(today):
    """期交所：三大法人台指期／小台／微台淨未平倉、大額交易人、VIX、臺指選擇權籌碼。"""
    path = os.path.join(DATA, "futures.json")
    KEYS = ("inst", "large", "vix", "opt", "optl")
    fut = {k: {} for k in KEYS}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            old = json.load(fh)
        for k in KEYS:
            fut[k] = old.get(k + "_map", {})
    start = today - dt.timedelta(days=180)
    # 只補最近 10 天＋缺的區段（以 30 天為一段查詢）
    def chunks(have_dates):
        last = max(have_dates) if have_dates else None
        s0 = start if not last else max(start, dt.date.fromisoformat(last) - dt.timedelta(days=10))
        cur = s0
        while cur <= today:
            e = min(today, cur + dt.timedelta(days=29))
            yield cur, e
            cur = e + dt.timedelta(days=1)
    for cid in ("TXF", "MXF", "TMF"):
        m = fut["inst"].setdefault(cid, {})
        for a, b in chunks(list(m.keys())):
            txt = get_raw(TAIFEX_INST_URL, {"queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d"), "commodityId": cid})
            time.sleep(PAUSE)
            if not txt or "查無資料" in txt:
                continue
            rows_ = parse_csv_text(txt)
            hh = rows_[0] if rows_ else []
            i_net = next((i for i, x in enumerate(hh) if "未平倉" in x and "口數" in x and "淨額" in x), 13)
            i_who = next((i for i, x in enumerate(hh) if "身份" in x), 2)
            for row in rows_[1:]:
                if len(row) <= i_net:
                    continue
                iso = row[0].replace("/", "-")
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", iso):
                    continue
                who = row[i_who]
                net = first_num(row[i_net])
                if net is None:
                    continue
                rec = m.setdefault(iso, [0, 0, 0])
                if "外資" in who:
                    rec[0] = net
                elif "投信" in who:
                    rec[1] = net
                elif "自營" in who:
                    rec[2] = net
        print(f"期貨三大法人 {cid}：{len(m)} 天", flush=True)
    lm = fut["large"]
    for a, b in chunks(list(lm.keys())):
        txt = get_raw(TAIFEX_LARGE_URL, {"queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d")})
        time.sleep(PAUSE)
        if not txt or "查無資料" in txt:
            continue
        best = {}
        for row in parse_csv_text(txt)[1:]:
            if len(row) < 10 or row[1].strip() != "TX":
                continue
            iso = row[0].replace("/", "-")
            vals = [first_num(x) for x in row[5:10]]
            if None in vals:
                continue
            all_type = row[4].strip() in ("0", "全部", "所有交易人")
            score = (1 if all_type else 0, vals[4], 1 if "999999" in row[3] else 0)
            if iso not in best or score > best[iso][0]:
                best[iso] = (score, vals)
        for iso, (_, v) in best.items():
            lm[iso] = [v[0] - v[1], v[2] - v[3], v[4]]
    print(f"大額交易人：{len(lm)} 天", flush=True)
    txt = get_raw(TAIFEX_VIX_URL)
    if txt:
        plain = re.sub(r"<[^>]+>", " ", txt)
        for dd, v in re.findall(r"(\d{4}/\d{1,2}/\d{1,2})\s+(\d{1,3}\.\d+)", plain):
            y, mo, da = dd.split("/")
            fut["vix"][f"{y}-{int(mo):02d}-{int(da):02d}"] = float(v)
    print(f"VIX：{len(fut['vix'])} 天", flush=True)

    def col(h, *keys, default=-1):
        for i, x in enumerate(h):
            if all(k in x for k in keys):
                return i
        return default

    # 三大法人臺指選擇權：淨留倉（口）＝買權淨未平倉 − 賣權淨未平倉
    om = fut["opt"]
    for a, b in chunks(list(om.keys())):
        txt = get_raw(TAIFEX_OPT_URL, {"queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d"), "commodityId": "TXO"})
        time.sleep(PAUSE)
        if not txt or "查無資料" in txt:
            continue
        rows = parse_csv_text(txt)
        if len(rows) < 2:
            continue
        h = rows[0]
        i_cp, i_who = col(h, "買賣權", default=2), col(h, "身份", default=3)
        i_net = col(h, "未平倉", "口數", "淨額", default=14)
        i_amt = col(h, "未平倉", "金額", "淨額", default=15)
        fresh = {}
        for row in rows[1:]:
            if len(row) <= max(i_net, i_amt):
                continue
            iso = row[0].replace("/", "-")
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", iso):
                continue
            sign = 1 if "買權" in row[i_cp] else -1 if "賣權" in row[i_cp] else 0
            who = row[i_who]
            k = 0 if "外資" in who else 1 if "投信" in who else 2 if "自營" in who else -1
            n, amt = first_num(row[i_net]), first_num(row[i_amt])
            if not sign or k < 0 or n is None:
                continue
            rec = fresh.setdefault(iso, [0, 0, 0, 0, 0, 0])
            rec[k] += sign * n
            rec[3 + k] += sign * (amt or 0)
        om.update(fresh)
    print(f"選擇權三大法人：{len(om)} 天", flush=True)

    # 大額交易人臺指選擇權：前五大、前十大淨留倉（買權淨 − 賣權淨）
    ol = fut["optl"]
    for a, b in chunks(list(ol.keys())):
        txt = get_raw(TAIFEX_OPT_LARGE_URL, {"queryStartDate": a.strftime("%Y/%m/%d"), "queryEndDate": b.strftime("%Y/%m/%d")})
        time.sleep(PAUSE)
        if not txt or "查無資料" in txt:
            continue
        rows = parse_csv_text(txt)
        if len(rows) < 2:
            continue
        h = rows[0]
        i_cp = col(h, "買賣權", default=3)
        i_mon = col(h, "到期月份", default=4)
        i_typ = col(h, "交易人類別", default=5)
        i_b5, i_s5 = col(h, "前五大", "買方", default=6), col(h, "前五大", "賣方", default=7)
        i_b10, i_s10 = col(h, "前十大", "買方", default=8), col(h, "前十大", "賣方", default=9)
        i_oi = col(h, "全市場", default=10)
        best = {}
        for row in rows[1:]:
            if len(row) <= i_oi or "TXO" not in [c.strip() for c in row[:3]]:
                continue
            iso = row[0].replace("/", "-")
            cp = "C" if "買權" in row[i_cp] else "P" if "賣權" in row[i_cp] else None
            vals = [first_num(row[i]) for i in (i_b5, i_s5, i_b10, i_s10, i_oi)]
            if not cp or None in vals:
                continue
            score = (1 if row[i_typ].strip() in ("0", "全部", "所有交易人") else 0, 1 if "999999" in row[i_mon] else 0, vals[4])
            key = (iso, cp)
            if key not in best or score > best[key][0]:
                best[key] = (score, vals)
        for (iso, cp), (_, v) in best.items():
            if cp == "C" and (iso, "P") in best:
                p = best[(iso, "P")][1]
                ol[iso] = [(v[0] - v[1]) - (p[0] - p[1]), (v[2] - v[3]) - (p[2] - p[3])]
    print(f"選擇權大額交易人：{len(ol)} 天", flush=True)

    keep = (today - dt.timedelta(days=400)).isoformat()
    out = {"updated": dt.datetime.now(TPE).strftime("%Y-%m-%d %H:%M")}
    for k in KEYS:
        if k == "inst":
            out["inst_map"] = {cid: {d: v for d, v in mp.items() if d >= keep} for cid, mp in fut["inst"].items()}
            out["inst"] = {cid: sorted([d] + v for d, v in mp.items()) for cid, mp in out["inst_map"].items()}
        else:
            out[k + "_map"] = {d: v for d, v in fut[k].items() if d >= keep}
            out[k] = sorted(([d] + v) if isinstance(v, list) else [d, v] for d, v in out[k + "_map"].items())
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(",", ":"))


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
        for code, v in day["p"].items():
            close, name = v[0], v[1]
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
    os.makedirs(DATA, exist_ok=True)
    if "--intraday" in sys.argv:
        intraday()
        return
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
    closes = update_taiex(today)
    build_market(closes)
    for step in (lambda: build_contrib(closes, update_shares()), lambda: update_futures(today)):
        try:
            step()
        except Exception as e:  # 單一功能失敗不影響其他資料
            print(f"略過：{e}", flush=True)


if __name__ == "__main__":
    main()
