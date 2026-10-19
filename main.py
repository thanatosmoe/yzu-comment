import asyncio
import getpass
import io
import json
import sys
import time
from pathlib import Path

from playwright.async_api import async_playwright

try:
    # 注意: 必须开启 line_buffering，否则 TextIOWrapper 默认块缓冲(约8KB)，
    # 交互模式下没有 input() 触发刷新时，所有 print 都不会显示，
    # 表现就是「选了操作后一直卡住」。
    sys.stdout = io.TextIOWrapper(
        sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
    sys.stderr = io.TextIOWrapper(
        sys.stderr.buffer, encoding="utf-8", errors="replace", line_buffering=True
    )
except Exception:
    pass

CONFIG_FILE = Path(__file__).parent / "config.json"

NET_MODES = {
    "webvpn": {
        "name": "外网 (WebVPN)",
        "entry": "https://webvpn.yzu.edu.cn/login?cas_login=true",
        "use_portal": True,
    },
    "intranet": {
        "name": "内网 (校园网直连)",
        "entry": "http://ydjwxs.yzu.edu.cn/",
        "use_portal": False,
    },
}


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        raise SystemExit(
            f"缺少配置文件 {CONFIG_FILE}，请创建：\n"
            '{\n  "comment_template": "主观评价内容"\n}\n'
        )
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def input_credentials() -> dict:
    print("\n请输入统一认证账号：")
    username = input("  学号: ").strip()
    if not username:
        raise SystemExit("学号不能为空")
    if sys.stdin.isatty():
        password = getpass.getpass("  密码: ")
    else:
        password = input("  密码: ").rstrip("\n")
    if not password:
        raise SystemExit("密码不能为空")
    return {"username": username, "password": password}


def choose_net_mode() -> str:
    print("请选择访问方式：")
    for key, m in NET_MODES.items():
        print(f"  {list(NET_MODES).index(key) + 1}. {m['name']}  ({m['entry']})")
    while True:
        choice = input("请输入编号: ").strip()
        if choice == "1":
            return "webvpn"
        if choice == "2":
            return "intranet"
        print("无效输入，请输入 1 或 2。")


def choose_action() -> tuple:
    print("\n请选择操作：")
    print("  1. 查看评测任务列表")
    print("  2. 自动评测所有待评估任务")
    print("  3. 演练(只查看将提交的答案，不真正提交)")
    while True:
        choice = input("请输入编号: ").strip()
        if choice == "1":
            return "list", False
        if choice == "2":
            return "run", False
        if choice == "3":
            return "run", True
        print("无效输入，请输入 1、2 或 3。")


class URP:
    def __init__(self, cfg: dict, net: str, cred: dict, headless: bool = True):
        self.cfg = cfg
        self.cred = cred
        self.net = net
        self.mode = NET_MODES[net]
        self.headless = headless
        self.base = None
        self.jw_page = None

    async def login(self) -> None:
        browser = await self._p.chromium.launch(
            headless=self.headless,
            args=[
                # 校园站点走直连，避免本地代理(127.0.0.1:8040)导致的
                # ERR_SSL_PROTOCOL_ERROR；仅绕过 *.yzu.edu.cn，其余仍走系统代理。
                "--proxy-bypass-list=*.yzu.edu.cn;<-loopback>",
            ],
        )
        self._browser = browser
        self._ctx = await browser.new_context()
        page = await self._ctx.new_page()

        print(f"[1/4] 通过 {self.mode['name']} 访问统一认证登录页 ...")
        await self._goto_with_retry(page, self.mode["entry"])
        await page.wait_for_selector('input[name="username"]', timeout=60000)
        await page.wait_for_timeout(2000)
        await page.fill('input[name="username"]', self.cred["username"])
        await page.fill('input[type="password"]', self.cred["password"])
        await page.wait_for_timeout(300)

        btn = page.locator('button[type="submit"], button').filter(has_text="登").first
        await btn.click()

        # 注意: 登录页 URL 本身就在 webvpn.yzu.edu.cn 下，
        # 不能用 wait_for_url(host) 判断登录完成，否则会立即返回。
        await self._wait_login_done(page)
        print("      统一认证登录成功")

        if self.mode["use_portal"]:
            await self._enter_via_portal(page)
        else:
            await self._enter_intranet(page)

    async def _wait_login_done(self, page, timeout: int = 60000):
        """等待真正离开登录/CAS 页面，返回落地后的 page。"""
        deadline = time.time() + timeout / 1000
        last_url = ""
        shown = 0
        while time.time() < deadline:
            for pg in self._ctx.pages:
                try:
                    u = pg.url or ""
                except Exception:
                    continue
                last_url = u
                if "sso.yzu.edu.cn" in u or "cas_login" in u:
                    continue
                if "/login" in u or "/cas/" in u:
                    continue
                if "webvpn.yzu.edu.cn" in u or "ydjwxs.yzu.edu.cn" in u:
                    return pg

            # 页面已给出明确错误时立即失败，不空等
            msg = await self._read_login_error(page)
            if msg:
                raise SystemExit(
                    "统一认证登录失败。\n"
                    f"      页面提示: {msg}\n"
                    "      常见原因: 账号或密码错误 / 需要验证码 / 登录过于频繁被临时限制。\n"
                    "      可改用 --headed 手动完成登录后再重试。"
                )

            shown += 1
            if shown % 5 == 0:
                print(f"      等待登录跳转 ... 已 {shown} 秒，当前: {last_url[:90]}")
            await page.wait_for_timeout(1000)

        raise SystemExit(
            "统一认证登录超时（可能账号/密码错误，或需要验证码）。\n"
            f"      停留在: {last_url}\n"
            "      可改用 --headed 手动处理验证码后重试。"
        )

    async def _read_login_error(self, page) -> str:
        """读取登录页上的错误提示，没有则返回空串。"""
        try:
            return await page.evaluate(
                """() => {
                    const bad = /(用户名或密码错误|密码错误|账号或密码|验证码|错误|失败|锁定|频繁)/;
                    const sels = [
                        '.error-msg', '.error-toast', '.ant-message-notice-content',
                        '.ant-form-item-explain-error', '.ant-notification-notice-message',
                        '[class*="error"]', '[class*="msg"]', '[class*="toast"]'
                    ];
                    for (const s of sels) {
                        for (const el of document.querySelectorAll(s)) {
                            const t = (el.innerText || '').trim();
                            if (!t || t.length >= 100) continue;
                            if (bad.test(t)) return t;
                            // 纯数字错误码，如 1030028
                            if (/^\\d{5,}$/.test(t)) return '错误码 ' + t;
                        }
                    }
                    return '';
                }"""
            ) or ""
        except Exception:
            return ""

    async def _goto_with_retry(self, page, url: str, tries: int = 3) -> None:
        last = None
        for i in range(1, tries + 1):
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=60000)
                return
            except Exception as e:
                last = e
                msg = str(e)
                print(f"      打开失败(第 {i}/{tries} 次): {msg.splitlines()[0][:120]}")
                if "ERR_SSL_PROTOCOL_ERROR" not in msg and "ERR_CONNECTION" not in msg:
                    raise
                await page.wait_for_timeout(1500 * i)
        raise last

    async def _enter_via_portal(self, page) -> None:
        print("[2/4] 打开教务系统学生端 ...")
        await page.wait_for_timeout(3000)

        target = await self._find_portal_link(page)
        if target is None:
            # 兜底: 允许用户在 config.json / 交互中输入 WebVPN 编码后的教务地址
            manual = self.cfg.get("jw_url") or ""
            if not manual and sys.stdin.isatty() and not self.headless:
                try:
                    manual = input(
                        "      未在门户中找到入口，可直接粘贴教务系统地址(留空跳过): "
                    ).strip()
                except Exception:
                    manual = ""
            if manual:
                await self._goto_with_retry(page, manual)
                await page.wait_for_timeout(3000)
                self.jw_page = page
                self.base = self._derive_base(page.url)
                print(f"      教务系统就绪: {self.base}")
                return

            debug = Path(__file__).parent / "portal_debug.html"
            try:
                debug.write_text(await page.content(), encoding="utf-8")
            except Exception:
                pass
            print("      门户页面上的链接（前 40 条）:")
            shown = 0
            for frame in page.frames:
                try:
                    for a in await frame.query_selector_all("a"):
                        t = ((await a.inner_text()) or "").strip().replace("\n", " ")
                        h = (await a.get_attribute("href") or "").strip()
                        if not (t or h):
                            continue
                        print(f"        - {t[:28]!r} -> {h[:80]}")
                        shown += 1
                        if shown >= 40:
                            break
                except Exception:
                    continue
                if shown >= 40:
                    break
            raise SystemExit(
                "门户中未找到教务系统入口。\n"
                f"      当前页面: {page.url}\n"
                f"      已保存页面快照: {debug}\n"
                "      可在 config.json 里加 \"jw_url\" 直接指定教务地址，"
                "或把上面的链接列表发给我。"
            )

        before = set(self._ctx.pages)
        try:
            async with self._ctx.expect_page(timeout=8000) as new_page_info:
                await target.click()
            await (await new_page_info.value).wait_for_load_state(
                "domcontentloaded", timeout=60000
            )
        except Exception:
            # 同标签页跳转
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=60000)
            except Exception:
                pass

        await page.wait_for_timeout(3000)

        # WebVPN 会把真实地址编码成 /http/<hex>/ 或 /https/<hex>/，
        # 新开页面优先，其次回退到任意带教务特征的页面。
        candidates = [pg for pg in self._ctx.pages if pg not in before]
        jw_page = None
        for pg in candidates:
            u = pg.url or ""
            title = ""
            try:
                title = await pg.title()
            except Exception:
                pass
            if "/http/" in u or "ydjwxs" in u or "教务" in title:
                jw_page = pg
                break
        if jw_page is None and candidates:
            jw_page = candidates[0]
        if jw_page is None:
            jw_page = page

        self.jw_page = jw_page
        self.base = self._derive_base(jw_page.url)
        print(f"      教务系统就绪: {self.base}")

    async def _find_portal_link(self, page):
        """在门户页（含 iframe）里找教务系统入口，最多等约 20 秒。"""
        for _ in range(20):
            for frame in page.frames:
                # 1) 常规 <a>，href 里带教务特征
                try:
                    for a in await frame.query_selector_all("a"):
                        t = ((await a.inner_text()) or "").strip()
                        href = (await a.get_attribute("href") or "").lower()
                        if "ydjw" in href or "/jw" in href:
                            return a
                        if "教务" in t:
                            return a
                except Exception:
                    pass

                # 2) SPA 门户：入口可能是 li/div/span，靠文本定位可点击祖先
                try:
                    loc = frame.locator(
                        "xpath=.//*[contains(text(),'教务系统学生端') "
                        "or contains(text(),'教务系统')]"
                    )
                    if await loc.count() > 0:
                        el = loc.first
                        clickable = el.locator(
                            "xpath=ancestor-or-self::*[self::a or self::li or self::div or self::span][1]"
                        )
                        return clickable
                except Exception:
                    pass

            await page.wait_for_timeout(1000)
        return None

    @staticmethod
    def _derive_base(url: str) -> str:
        u = (url or "").split("?")[0].split("#")[0].rstrip("/")
        for cut in ("/student/", "/index", "/login"):
            i = u.find(cut)
            if i > 0:
                u = u[:i]
        return u.rstrip("/")

    async def _enter_intranet(self, page) -> None:
        print("[2/4] 进入教务系统 ...")
        await page.wait_for_timeout(3000)
        self.jw_page = page
        self.base = page.url.rstrip("/")
        print(f"      教务系统就绪: {self.base}")

    async def list_evaluations(self, flag: str = "ktjs") -> list:
        api = self.base + "/student/teachingAssessment/evaluation/queryAll"
        resp = await self.jw_page.evaluate(
            """async (args) => {
                const r = await fetch(args.api, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                    body: 'pageNum=1&pageSize=200&flag=' + args.flag
                });
                return await r.text();
            }""",
            {"api": api, "flag": flag},
        )
        data = json.loads(resp)
        return (data.get("data") or {}).get("records") or []

    async def open_evaluation(self, ktid: str) -> dict:
        url = self.base + f"/student/teachingEvaluation/newEvaluation/evaluation/{ktid}"
        await self.jw_page.goto(url, wait_until="networkidle", timeout=60000)
        await self.jw_page.wait_for_timeout(2000)
        return await self._extract_form()

    async def read_countdown(self) -> int:
        """读取评测页上「还需停留多少秒」的提示，读不到返回 0。"""
        try:
            return await self.jw_page.evaluate(
                """() => {
                    const num = (s) => { const v = parseInt(s, 10); return (v >= 0 && v <= 3600) ? v : null; };
                    const texts = [];
                    // 1) 优先看带计时语义的元素
                    document.querySelectorAll(
                        '[id*="time" i],[id*="timer" i],[id*="dao" i],[id*="jishi" i],'
                        + '[class*="time" i],[class*="count" i],[class*="timer" i],[class*="clock" i]'
                    ).forEach(el => { const t = (el.innerText || '').trim(); if (t && t.length < 120) texts.push(t); });
                    // 2) 正文里提到「秒/计时/倒计时/剩余」的短句
                    const body = document.body ? document.body.innerText || '' : '';
                    body.split('\\n').forEach(line => {
                        const t = line.trim();
                        if (t && t.length < 120 && /(秒|分钟|倒计时|计时|剩余)/.test(t)) texts.push(t);
                    });
                    for (const t of texts) {
                        let m = t.match(/(\\d+)\\s*秒/);
                        if (m) { const v = num(m[1]); if (v !== null) return v; }
                        m = t.match(/(\\d+)\\s*分(?:钟)?/);
                        if (m) { const v = num(m[1]); if (v !== null) return v * 60; }
                        m = t.match(/剩余\\D{0,4}(\\d+)/);
                        if (m) { const v = num(m[1]); if (v !== null) return v; }
                        m = t.match(/(\\d{1,2})\\s*[:：]\\s*(\\d{2})/);
                        if (m) { const v = num(m[1]) * 60 + num(m[2]); if (v !== null) return v; }
                    }
                    return 0;
                }"""
            )
        except Exception:
            return 0

    async def wait_page_timer(self, extra: int = 3, cap: int = 600) -> int:
        """等页面倒计时走完。返回等待的秒数(0 表示页面上没有计时要求)。"""
        secs = await self.read_countdown()
        if secs <= 0:
            # 页面上读不到计时文本时，按配置的固定停留时间兜底
            fallback = int(self.cfg.get("min_wait_seconds", 0) or 0)
            if fallback <= 0:
                return 0
            print(f"    未读到页面计时，按配置停留 {fallback} 秒 ...")
            await self.jw_page.wait_for_timeout(fallback * 1000)
            return fallback
        total = min(secs, cap)
        print(f"    页面要求停留计时，等待 {total} 秒 ...")
        waited = 0
        while waited < total:
            step = min(5, total - waited)
            await self.jw_page.wait_for_timeout(step * 1000)
            waited += step
            left = await self.read_countdown()
            if 0 < left < total - waited:
                # 页面计时更慢/更快，按页面为准
                total = waited + min(left, cap)
        await self.jw_page.wait_for_timeout(extra * 1000)
        return waited

    async def _extract_form(self) -> dict:
        return await self.jw_page.evaluate(
            """() => {
                const form = document.getElementById('saveEvaluation');
                if (!form) return {error: '未找到评测表单(可能已评估或已过期)'};
                const out = {hidden: {}, questions: [], textarea: null};
                form.querySelectorAll('input[type="hidden"]').forEach(i => out.hidden[i.name] = i.value);
                const qmap = {};
                form.querySelectorAll('input').forEach(i => {
                    if (i.type === 'radio' || i.type === 'checkbox') {
                        if (!qmap[i.name]) qmap[i.name] = {type: i.type, options: []};
                        qmap[i.name].options.push({value: i.value, label: (i.parentElement ? i.parentElement.innerText : '').replace(/\\s+/g,' ').trim()});
                    }
                });
                out.questions = Object.keys(qmap).map(k => ({name: k, type: qmap[k].type, options: qmap[k].options}));
                const ta = form.querySelector('textarea');
                out.textarea = ta ? {name: ta.name, maxlength: ta.maxLength} : null;
                return out;
            }"""
        )

    async def submit(self, form: dict, comment: str, dry_run: bool = False) -> dict:
        token = form["hidden"].get("tokenValue", "")
        payload = {}
        for q in form["questions"]:
            if q["type"] == "radio":
                payload[q["name"]] = q["options"][0]["value"]
            elif q["type"] == "checkbox":
                payload[q["name"]] = [o["value"] for o in q["options"]]
        if form.get("textarea"):
            payload[form["textarea"]["name"]] = comment

        if dry_run:
            return {"result": "dry-run", "payload": payload, "token": token}

        result = await self.jw_page.evaluate(
            """async (args) => {
                const fd = new FormData();
                fd.append('tjcs', args.hidden.tjcs || '1');
                fd.append('wjbm', args.hidden.wjbm || '');
                fd.append('ktid', args.hidden.ktid || '');
                fd.append('compare', '');
                for (const [k, v] of Object.entries(args.payload)) {
                    if (Array.isArray(v)) { for (const x of v) fd.append(k, x); }
                    else fd.append(k, v);
                }
                const r = await fetch('/student/teachingAssessment/baseInformation/questionsAdd/doSave?tokenValue=' + args.token, {
                    method: 'POST', body: fd
                });
                return await r.text();
            }""",
            {"payload": payload, "token": token, "hidden": form["hidden"]},
        )
        return json.loads(result)

    async def close(self) -> None:
        if hasattr(self, "_browser"):
            await self._browser.close()


async def cmd_list(cfg: dict, net: str, cred: dict, headless: bool) -> int:
    urp = URP(cfg, net=net, cred=cred, headless=headless)
    async with async_playwright() as p:
        urp._p = p
        try:
            await urp.login()
            records = await urp.list_evaluations("ktjs")
            if not records:
                print("当前无评测任务。")
                return 0
            pending = [r for r in records if r.get("SFPG") != "1"]
            print(f"课堂教师评测: 共 {len(records)} 条, 待评估 {len(pending)} 条\n")
            for r in records:
                mark = "待评估" if r.get("SFPG") != "1" else "已评估"
                print(f"  [{mark}] {r['JSM']:8s} | {r['KCM']} | 课程号 {r['KCH']}-{r['KXH']}")
        finally:
            await urp.close()
    return 0


async def cmd_run(cfg: dict, net: str, cred: dict, headless: bool, dry_run: bool, filter_kw: str = "") -> int:
    urp = URP(cfg, net=net, cred=cred, headless=headless)
    async with async_playwright() as p:
        urp._p = p
        try:
            await urp.login()
            records = await urp.list_evaluations("ktjs")
            pending = [r for r in records if r.get("SFPG") != "1"]
            if filter_kw:
                kw = filter_kw.lower()
                pending = [r for r in pending if kw in (r["JSM"] + r["KCM"]).lower()]
            if not pending:
                print("没有待评估的评测任务。" if not filter_kw else f"没有匹配「{filter_kw}」的待评估任务。")
                return 0

            print(f"\n开始评测 {len(pending)} 门课程 ...\n")
            ok = 0
            for i, rec in enumerate(pending, 1):
                print(f"[{i}/{len(pending)}] {rec['JSM']} - {rec['KCM']}")
                form = await urp.open_evaluation(rec["KTID"])
                if "error" in form:
                    print(f"    跳过: {form['error']}")
                    continue

                # 教务系统要求打开页面后停留到倒计时结束才允许保存
                waited = await urp.wait_page_timer()
                if waited == 0 and not dry_run:
                    print("    未检测到页面计时，直接提交")

                res = await urp.submit(form, cfg["comment_template"], dry_run=dry_run)
                if dry_run:
                    print(f"    [dry-run] 题目 {len(res['payload'])} 道")
                    for k, v in res["payload"].items():
                        if isinstance(v, list):
                            print(f"      {k[:24]} 多选 {len(v)} 项: {v[0][:14]} ...")
                        else:
                            print(f"      {k[:24]} = {v[:20]}")
                    ok += 1
                else:
                    msg = res.get("msg2") or res.get("msg") or res.get("result") or res
                    if res.get("result") == "ok":
                        print("    ✓ 评估成功")
                        ok += 1
                    else:
                        print(f"    ✗ {msg}")
                        if "计时" in str(msg):
                            print("    提示: 页面计时未走完，本次跳过，稍后可单独重跑该课程")
                await urp.jw_page.wait_for_timeout(1500)
            print(f"\n完成: 成功 {ok}/{len(pending)}")
        finally:
            await urp.close()
    return 0


async def cmd_probe(cfg: dict, net: str, cred: dict, headless: bool, filter_kw: str = "") -> int:
    """打开第一门待评估课，打印计时相关信息，不提交。"""
    urp = URP(cfg, net=net, cred=cred, headless=headless)
    async with async_playwright() as p:
        urp._p = p
        try:
            await urp.login()
            records = await urp.list_evaluations("ktjs")
            pending = [r for r in records if r.get("SFPG") != "1"]
            if filter_kw:
                kw = filter_kw.lower()
                pending = [r for r in pending if kw in (r["JSM"] + r["KCM"]).lower()]
            if not pending:
                print("没有待评估的任务可探测。")
                return 0
            rec = pending[0]
            print(f"探测: {rec['JSM']} - {rec['KCM']}")
            await urp.open_evaluation(rec["KTID"])

            info = await urp.jw_page.evaluate(
                """() => {
                    const out = {timerish: [], lines: []};
                    document.querySelectorAll(
                        '[id*="time" i],[id*="timer" i],[id*="dao" i],[id*="jishi" i],'
                        + '[class*="time" i],[class*="count" i],[class*="timer" i],[class*="clock" i]'
                    ).forEach(el => {
                        const t = (el.innerText || '').trim();
                        if (t && t.length < 120) out.timerish.push({
                            tag: el.tagName, id: el.id,
                            cls: (el.className && el.className.toString ? el.className.toString() : '').slice(0, 60),
                            text: t.slice(0, 120)
                        });
                    });
                    const body = document.body ? document.body.innerText || '' : '';
                    body.split('\\n').forEach(line => {
                        const t = line.trim();
                        if (t && t.length < 120 && /(秒|分钟|倒计时|计时|剩余)/.test(t)) out.lines.push(t);
                    });
                    return out;
                }"""
            )
            print("\n--- 计时相关元素 ---")
            for x in info["timerish"][:20]:
                print(f"  <{x['tag']} id={x['id']!r} cls={x['cls']!r}> {x['text']!r}")
            print("\n--- 含 秒/计时/剩余 的文本行 ---")
            for t in info["lines"][:20]:
                print(f"  {t!r}")

            secs = await urp.read_countdown()
            print(f"\n解析结果: 需要停留 {secs} 秒" if secs else "\n解析结果: 未识别到计时")
            if not headless:
                print("浏览器保持打开 120 秒，可手动查看页面上的计时提示 ...")
                await urp.jw_page.wait_for_timeout(120000)
        finally:
            await urp.close()
    return 0


def usage() -> None:
    print(
        "用法:\n"
        "  python main.py                   交互模式(选网络→输入账号密码→选操作)\n"
        "  python main.py list --net 1      外网 查看评测任务列表\n"
        "  python main.py run --net 2       内网 自动评测所有待评估任务\n"
        "  python main.py run --net 1 --dry-run   外网 演练(不真正提交)\n"
        "  python main.py run --net 1 -k 张三    外网 只评测姓名/课程名包含“张三”的任务\n"
        "  python main.py run --net 1 --headed   外网 显示浏览器窗口(如遇验证码时手动处理)\n"
        "  python main.py probe --net 1        探测评测页的停留计时结构(不提交)\n\n"
        "所有模式下都会提示输入统一认证学号和密码。\n"
        "网络方式: --net 1 = 外网(WebVPN), --net 2 = 内网(校园网直连)\n"
        "\n"
        "说明: 教务系统要求打开评测页后停留到页面倒计时结束才允许保存。\n"
        "程序会自动读取页面倒计时并等待，无需手工计时；\n"
        "若页面计时无法识别，可在 config.json 设置 \"min_wait_seconds\" 指定固定停留秒数。\n"
    )


async def main():
    args = sys.argv[1:]
    cfg = load_config()
    dry_run = "--dry-run" in args
    headed = "--headed" in args
    filter_kw = ""
    if "-k" in args:
        filter_kw = args[args.index("-k") + 1]
    headless = not headed

    net = None
    if "--net" in args:
        v = args[args.index("--net") + 1]
        net = "webvpn" if v == "1" else "intranet"

    cmd = None
    if args and args[0] in ("list", "run", "probe"):
        cmd = args[0]
    elif args and args[0] in ("-h", "--help", "help"):
        usage()
        return 0

    if cmd is None:
        if net is None:
            net = choose_net_mode()
        cred = input_credentials()
        cmd, dry_run = choose_action()
        if cmd == "list":
            return await cmd_list(cfg, net, cred, headless)
        if cmd == "run":
            return await cmd_run(cfg, net, cred, headless, dry_run, filter_kw)

    cred = input_credentials()

    if cmd == "list":
        return await cmd_list(cfg, net, cred, headless)
    if cmd == "run":
        return await cmd_run(cfg, net, cred, headless, dry_run, filter_kw)
    if cmd == "probe":
        return await cmd_probe(cfg, net, cred, headless, filter_kw)
    usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
