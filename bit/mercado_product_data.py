"""Shared, bounded listing cache backed by a private account-owned upstream session.

Customers authenticate with Zeshun. Vendor runtime/session remain on the server.
SQLite + an advisory lock coalesce misses across threads and service processes.
"""
import atexit
import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import sqlite3
import subprocess
import threading
import time

SITES = {"MLM": 1, "MLB": 2, "MLC": 3, "MCO": 4, "MLA": 5, "MLU": 6}
CURRENCIES = {"MLM": "MXN", "MLB": "BRL", "MLC": "CLP", "MCO": "COP", "MLA": "ARS", "MLU": "UYU"}
FULFILLMENT = {1: ("self_ship", "自发货"), 2: ("overseas", "Full海外仓"),
               3: ("local", "本土仓"), 4: ("semi_managed", "半托管")}
MESSAGES = {401: "数据源会话已过期，请联系泽顺管理员更新", 403: "数据源账号权限不足，请联系泽顺管理员",
            429: "数据源暂时限频，请稍后重试", 502: "商品数据源暂时不可用，请稍后重试"}


class DataError(Exception):
    def __init__(self, message, status=503):
        super().__init__(message)
        self.status = status


def number(value, positive=False):
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 and (not positive or n > 0) else None
    except (ValueError, TypeError):
        return None


def normalize(row):
    size = row.get("Size")
    size = [number(v, True) for v in size] if isinstance(size, list) and len(size) == 3 else []
    size = size if size and all(v is not None for v in size) else None
    kind, label = FULFILLMENT.get(number(row.get("Storagetype")), ("unknown", "未识别"))
    country = str(row.get("SellerAddressCountry") or "").upper()
    country = {"CHINA": "CN", "UNITED STATES": "US"}.get(country, country)
    return dict(sku=str(row.get("Sku") or ""), weight_g=number(row.get("Weight"), True), size_cm=size,
                volumetric_weight_g=math.floor(math.prod(size) / 6 + .5) if size else None,
                listed_at=row.get("Uptime"), commission_percent=number(row.get("Fee")),
                brand=str(row.get("Brand") or ""), seller=str(row.get("Sellername") or row.get("SellerName") or ""),
                seller_id=row.get("SellerId"), seller_country=country,
                seller_level=number(row.get("SellerLevel")), seller_status=row.get("PowerSellerStatus"),
                sales=number(row.get("Orders")), fulfillment_type=kind, fulfillment_label=label,
                fulfillment_eligible=kind in {"self_ship", "semi_managed"} and country != "US")


def estimate(info, price, freight, rates):
    """Matches the inspected card's defaults; disclosed as an estimate, not settlement."""
    prefix = info["sku"][:3]
    price = number(price, True)
    usd_rate, local_rate = number(rates.get("USD"), True), number(rates.get(CURRENCIES.get(prefix)), True)
    price_usd = price * local_rate / usd_rate if price and usd_rate and local_rate else None
    threshold = {"MLM": 299, "MLB": 79, "MLC": 19990, "MCO": 60000, "MLA": 33000}.get(prefix)
    low = price < threshold if price and threshold else None
    bands = sorted([r for r in freight if str(r.get("abbr", "")).upper() == prefix
                    and number(r.get("start")) is not None and number(r.get("ended"), True) is not None],
                   key=lambda r: float(r["start"]))
    def cost(weight):
        if weight is None or low is None or not bands:
            return None
        row = next((r for r in bands if float(r["start"]) <= weight < float(r["ended"])), None)
        if row is None and weight >= float(bands[-1]["ended"]):
            row = bands[-1]
        return number(row.get("below_freight" if low else "freight")) if row else None
    weight, volume = info["weight_g"], info["volumetric_weight_g"]
    actual, volumetric = cost(weight), cost(volume)
    if actual is not None and volumetric is not None:
        selected = "weight" if weight <= 500 or weight > (volume or 0) or actual > volumetric else "size"
    else:
        selected = "weight" if actual is not None else "size" if volumetric is not None else None
    shipping = actual if selected == "weight" else volumetric
    # The inspected ZYing card displays Fee but calculates default revenue with 15%.
    commission = price_usd * .15 if price_usd is not None else None
    net = max(price_usd - commission - shipping, 3) if commission is not None and shipping is not None else None
    return dict(price_usd=price_usd, weight_freight_usd=actual, size_freight_usd=volumetric,
                effective_freight_source=selected, low_freight=low, net_proceeds_usd=round(net, 2) if net is not None else None,
                net_proceeds_cny=round(net * usd_rate / 100, 2) if net is not None else None,
                commission_amount_usd=commission, estimate_commission_percent=15,
                estimate_note="参考智赢默认卡片：售价按汇率折美元，扣15%佣金及运费，最低显示3美元；不代表实际结算或采购利润")


class ProductDataService:
    def __init__(self, directory=None, provider=None):
        self.directory = Path(directory or os.environ.get("ZESHUN_MERCADO_CACHE_DIR") or
                              Path(__file__).resolve().parents[1] / ".data/mercado-product-data")
        self.provider = provider
        self.lock = threading.Lock()
        self.process = None
        self.pid = os.getpid()
        self.renewal_stop = threading.Event()
        self.renewal_thread = None
        atexit.register(self.close)

    def close(self):
        self.renewal_stop.set()
        if self.process:
            self.process.kill()
            self.process.wait()
            self.process = None

    def configured(self):
        return bool(self.provider or (os.environ.get("ZESHUN_MERCADO_SESSION_FILE") and
                    Path(os.environ["ZESHUN_MERCADO_SESSION_FILE"]).is_file() and
                    Path(os.environ.get("ZESHUN_MERCADO_EXTENSION_DIR", ""), "assets/js/background.js").is_file() and
                    shutil.which(os.environ.get("ZESHUN_MERCADO_NODE", "node"))))

    def status(self):
        ready = self.configured()
        renewal = {"state": "not_configured"}
        if ready:
            with self.database() as db:
                renewal = self._renewal_state(db)
                if renewal.get("session_stamp") != self._session_stamp():
                    renewal = {"state": "pending"}
        requires_login = renewal.get("state") == "requires_login"
        return {"ready": ready and not requires_login,
                "message": "数据源会话无法续期，请联系泽顺管理员重新登录" if requires_login else
                    "已配置泽顺商品数据服务，客户无需登录智赢" if ready else "管理员尚未配置商品数据服务",
                "customer_zying_login_required": False,
                "renewal": {k:v for k,v in renewal.items() if k != "session_stamp"}}

    @contextlib.contextmanager
    def database(self):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.lock.acquire(timeout=40):
            raise DataError("商品数据服务繁忙，请稍后重试")
        try:
            with (self.directory / "query.lock").open("a") as lockfile:
                end = time.monotonic() + 40
                while True:
                    try:
                        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() > end:
                            raise DataError("商品数据服务繁忙，请稍后重试")
                        time.sleep(.05)
                try:
                    with sqlite3.connect(self.directory / "cache.sqlite3", timeout=5) as db:
                        db.execute("CREATE TABLE IF NOT EXISTS cache (key TEXT PRIMARY KEY, expires REAL, value TEXT)")
                        db.execute("CREATE TABLE IF NOT EXISTS session_renewal (id INTEGER PRIMARY KEY, value TEXT)")
                        try:
                            yield db
                        finally:
                            # Persist cooldowns even if the caller exits with an upstream error.
                            db.commit()
                finally:
                    fcntl.flock(lockfile, fcntl.LOCK_UN)
        finally:
            self.lock.release()

    def call(self, message):
        if self.provider:
            return self.provider(message)
        if self.pid != os.getpid():
            self.process = None
            self.pid = os.getpid()
        if not self.process or self.process.poll() is not None:
            self.process = subprocess.Popen([os.environ.get("ZESHUN_MERCADO_NODE", "node"),
                str(Path(__file__).with_name("mercado_data_runtime") / "worker.cjs")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            self.process.stdin.write((json.dumps(message) + "\n").encode())
            self.process.stdin.flush()
            with selectors.DefaultSelector() as selector:
                selector.register(self.process.stdout, selectors.EVENT_READ)
                if not selector.select(16):
                    raise TimeoutError()
            result = json.loads(self.process.stdout.readline())
            if not result.get("ok"):
                return None, result.get("code", 502)
            return result["data"], None
        except (OSError, ValueError, TimeoutError):
            if self.process:
                self.process.kill()
                self.process.wait()
                self.process = None
            return None, 502

    @staticmethod
    def _session_stamp():
        filename = os.environ.get("ZESHUN_MERCADO_SESSION_FILE")
        try:
            stat = os.stat(filename) if filename else None
            return str(stat.st_mtime_ns) + ":" + str(stat.st_ino) if stat else "test"
        except OSError:
            return "missing"

    @staticmethod
    def _renewal_state(db):
        row = db.execute("SELECT value FROM session_renewal WHERE id=1").fetchone()
        return json.loads(row[0]) if row else {"state":"pending"}

    @staticmethod
    def _save_renewal(db, state):
        db.execute("INSERT OR REPLACE INTO session_renewal VALUES (1,?)", (json.dumps(state),))

    @staticmethod
    def renewal_interval():
        try:
            return max(300, min(86400, int(os.environ.get("ZESHUN_MERCADO_RENEW_SECONDS", "1800"))))
        except ValueError:
            return 1800

    def _renew_session(self, db, *, force=False):
        stamp = self._session_stamp()
        state = self._renewal_state(db)
        if state.get("session_stamp") != stamp:
            state = {"state":"pending", "session_stamp":stamp, "failures":0}
        now = time.time()
        # Revoked credentials require a new administrator login. Never hammer auth.
        if state.get("state") == "requires_login":
            return state
        if now < state.get("next_attempt", 0) and (not force or state.get("state") == "retry_wait"):
            return state
        data, error = self.call({"action":"refresh"})
        if not error and not (isinstance(data, dict) and (data.get("renewed") or data.get("superseded"))):
            error = 502
        if not error and data.get("superseded"):
            state = {"state":"pending", "session_stamp":self._session_stamp(), "next_attempt":now+30, "failures":0}
        elif not error:
            state = {"state":"healthy", "session_stamp":self._session_stamp(), "last_success":now,
                     "next_attempt":now+self.renewal_interval(), "failures":0}
            # A successful renewal invalidates obsolete auth-failure cooldowns.
            db.execute("DELETE FROM cache WHERE key LIKE 'cooldown:%'")
            logging.info("泽顺商品数据源自动续期成功")
        else:
            failures = state.get("failures", 0) + 1
            delay = max(900 if error == 429 else 60, min(1800, 60 * 2 ** min(failures-1, 5)))
            state = {**state, "state":"requires_login" if error in (401,403) else "retry_wait",
                     "failures":failures, "last_attempt":now, "error_code":error,
                     "next_attempt":now+delay}
            logging.warning("泽顺商品数据源续期失败，状态=%s，错误码=%s", state["state"], error)
        self._save_renewal(db, state)
        return state

    def renew_session(self):
        if not self.configured():
            return {"state":"not_configured"}
        with self.database() as db:
            return {k:v for k,v in self._renew_session(db).items() if k != "session_stamp"}

    def _authenticated_call(self, db, message):
        state = self._renewal_state(db)
        if state.get("state") == "requires_login" and state.get("session_stamp") == self._session_stamp():
            return None, 401
        data, error = self.call(message)
        if error != 401:
            return data, error
        # One normal refresh, followed by at most one replay of a read-only query.
        state = self._renew_session(db, force=True)
        if state.get("state") == "healthy":
            data, error = self.call(message)
            if error == 401:
                self._save_renewal(db, {**state, "state":"requires_login", "error_code":401})
        return data, error

    def start_renewal_scheduler(self):
        if self.renewal_thread and self.renewal_thread.is_alive():
            return
        self.renewal_stop.clear()
        def run():
            while not self.renewal_stop.is_set():
                try:
                    self.renew_session()
                except Exception:
                    # No session file contents or upstream response are logged.
                    logging.warning("泽顺商品数据源续期调度暂不可用，将稍后重试")
                self.renewal_stop.wait(60)
        self.renewal_thread = threading.Thread(target=run, name="mercado-data-session-renewal", daemon=True)
        self.renewal_thread.start()

    @staticmethod
    def get(db, key):
        row = db.execute("SELECT value FROM cache WHERE key=? AND expires>?", (key, time.time())).fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def put(db, key, value, ttl):
        db.execute("INSERT OR REPLACE INTO cache VALUES (?,?,?)", (key, time.time()+ttl, json.dumps(value)))

    def _metadata(self, db, action):
        value = self.get(db, "meta:" + action)
        if value is None or not value.get("fetched_at"):
            data, error = self._authenticated_call(db, {"action": action})
            value = {"data": data if not error else None, "fetched_at": time.time()}
            self.put(db, "meta:" + action, value, 1800 if not error else 60)
        return value

    def exchange_rates(self):
        """Latest upstream reference snapshot, shared with price estimates for 30 minutes."""
        if not self.configured():
            raise DataError("管理员尚未配置商品数据服务")
        with self.database() as db:
            snapshot = self._metadata(db, "exchange")
        raw = snapshot.get("data")
        usd = number(raw.get("USD"), True) if isinstance(raw, dict) else None
        if not usd:
            raise DataError("美元参考汇率暂不可用，请稍后重试")
        rates = {currency: value / usd for currency in CURRENCIES.values()
                 if (value := number(raw.get(currency), True)) is not None}
        return {"base": "USD", "rates_to_usd": {**rates, "USD": 1.0},
                "fetched_at": snapshot["fetched_at"],
                "expires_at": snapshot["fetched_at"] + 1800,
                "source": "泽顺商品数据服务（智赢参考汇率）"}

    def query(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 50:
            raise DataError("每次查询须包含1–50个商品", 400)
        requested = {}
        inputs = []
        for item in items:
            if not isinstance(item, dict):
                raise DataError("商品参数无效", 400)
            sku = str(item.get("sku") or "").replace("-", "").upper()
            if not re.fullmatch(r"(?:MLM|MLB|MLC|MCO|MLA|MLU)\d{5,}", sku):
                raise DataError("需要商品刊登编号，不能使用目录或变体组编号", 400)
            requested[sku] = item.get("price")
            inputs.append((sku, number(item.get("price"), True)))
        if not self.configured():
            raise DataError("管理员尚未配置商品数据服务")
        with self.database() as db:
            # Rotating the private session immediately clears its failure cooldown.
            stamp = self._session_stamp()
            cooldown_key = "cooldown:" + hashlib.sha256(stamp.encode()).hexdigest()[:16]
            cached = {sku: self.get(db, "item:"+sku) for sku in requested}
            misses = [sku for sku in requested if cached[sku] is None]
            for site in sorted({SITES[sku[:3]] for sku in misses}):
                error = self.get(db, cooldown_key)
                if error:
                    raise DataError(MESSAGES.get(error, MESSAGES[502]))
                skus = [sku for sku in misses if SITES[sku[:3]] == site]
                rows, error = self._authenticated_call(db, {"action":"products", "skus":skus, "site":site})
                if error:
                    cooldown_key = "cooldown:" + hashlib.sha256(self._session_stamp().encode()).hexdigest()[:16]
                    self.put(db, cooldown_key, error, 300 if error in (401,403,429) else 15)
                    raise DataError(MESSAGES.get(error, MESSAGES[502]))
                by_id = {str(row.get("Sku")): normalize(row) for row in rows if isinstance(row, dict)}
                for sku in skus:
                    value = {"product":by_id.get(sku), "fetched_at":time.time()}
                    self.put(db, "item:"+sku, value, 600 if value["product"] else 45)
                    cached[sku] = value
            metadata = {}
            warnings = []
            for action in ("freight", "exchange"):
                value = self._metadata(db, action)
                metadata[action] = value["data"]
                if value["data"] is None:
                    warnings.append("运费表暂不可用" if action == "freight" else "汇率暂不可用")
            results = []
            for sku, price in inputs:
                value = cached[sku]
                info = value["product"]
                if info:
                    info = {**info, **estimate(info, price, metadata["freight"] or [], metadata["exchange"] or {})}
                results.append({"sku":sku, "requested_price":price, "product":info, "cached":sku not in misses, "fetched_at":value["fetched_at"]})
            db.execute("DELETE FROM cache WHERE expires<?", (time.time(),))
            db.execute("DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY expires DESC LIMIT -1 OFFSET 20000)")
            return {"items":results, "warnings":warnings, "source":"zeshun_server"}


service = ProductDataService()
