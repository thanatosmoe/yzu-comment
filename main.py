import asyncio
import getpass
import io
import json
import sys
from pathlib import Path

from playwright.async_api import async_playwright

try:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
except Exception:
    pass

CONFIG_FILE = Path(__file__).parent / "config.json"

NET_MODES = {
    "webvpn": {
        "name": "外网 (WebVPN)",
        "entry": "https://webvpn.yzu.edu.cn/login?cas_login=true",
        "host": "**/webvpn.yzu.edu.cn/**",
        "use_portal": True,
    },
    "intranet": {
        "name": "内网 (校园网直连)",
        "entry": "http://ydjwxs.yzu.edu.cn/",
        "host": "**/ydjwxs.yzu.edu.cn/**",
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
        browser = await self._p.chromium.launch(headless=self.headless)
        self._browser = browser
        self._ctx = await browser.new_context()
        page = await self._ctx.new_page()

        print(f"[1/4] 通过 {self.mode['name']} 访问统一认证登录页 ...")
        await page.goto(self.mode["entry"], wait_until="domcontentloaded", timeout=60000)
        await page.wait_for_selector('input[name="username"]', timeout=60000)
        await page.wait_for_timeout(2000)
        await page.fill('input[name="username"]', self.cred["username"])
        await page.fill('input[type="password"]', self.cred["password"])
        await page.wait_for_timeout(300)
        btn = page.locator("button").filter(has_text="登").first
        await btn.click()
        await page.wait_for_url(self.mode["host"], timeout=60000)
        await page.wait_for_timeout(3000)
        print("      统一认证登录成功")

        if self.mode["use_portal"]:
            await self._enter_via_portal(page)
        else:
            await self._enter_intranet(page)

    async def _enter_via_portal(self, page) -> None:
        print("[2/4] 打开教务系统学生端 ...")
        clicked = False
        for a in await page.query_selector_all("a"):
            t = (await a.inner_text() or "").strip()
            if "教务系统学生端" in t:
                await a.click()
                clicked = True
                break
        if not clicked:
            raise SystemExit("门户中未找到「教务系统学生端」入口")
        await page.wait_for_timeout(8000)
        jw_page = None
        for pg in self._ctx.pages:
            if "/http/" in pg.url:
                jw_page = pg
                break
        if jw_page is None:
            raise SystemExit("未能打开教务系统页面")
        self.jw_page = jw_page
        self.base = jw_page.url.replace("/index", "")
        print(f"      教务系统就绪: {self.base}")

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
                        print(f"    ✓ 评估成功")
                        ok += 1
                    else:
                        print(f"    ✗ {msg}")
                await urp.jw_page.wait_for_timeout(1500)
            print(f"\n完成: 成功 {ok}/{len(pending)}")
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
        "  python main.py run --net 1 --headed   外网 显示浏览器窗口(如遇验证码时手动处理)\n\n"
        "所有模式下都会提示输入统一认证学号和密码。\n"
        "网络方式: --net 1 = 外网(WebVPN), --net 2 = 内网(校园网直连)\n"
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
    if args and args[0] in ("list", "run"):
        cmd = args[0]
    elif args and args[0] in ("-h", "--help", "help"):
        usage()
        return 0

    if cmd is None:
        if net is None:
            net = choose_net_mode()
        if args and args[0] in ("-h", "--help", "help"):
            usage()
            return 0
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
    usage()
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
