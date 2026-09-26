"""launcher 的**鉴权就绪**逻辑 —— 「一键启动」不能撞上"配了鉴权却没密钥"的启动失败。

## 这条用例的由来（2026-09-23 实测踩坑）

README 与 `launcher.py` 的用法段都把 `python launcher.py` 写成**一条命令启动**。
但批次 A 之后：`FA_AUTH_ENABLED` 默认 **1**（安全默认），而 `FA_JWT_SECRET` 为空时
服务在 lifespan 里 `require_jwt_secret()` **启动即失败** —— 于是这条主入口必然报
`RuntimeError`，而 `launcher.py --check` 还照样说「预检通过」：预检清单里根本没有
这个**启动级前置条件**。

本文件守三件事：

1. `--check` 必须能看见它（不能报"通过"却一起就崩）；
2. 本机启动必须能**自愈**：密钥缺失时现生成一份写进 gitignore 的
   `data/db_keys.local.json`（与 `scripts/deploy_vm.py` 给 VM 侧生成 `.env` 同一做法），
   演示账号口令同理 —— 否则页面卡在登录页，谁也进不来；
3. **源码与文档里不写死任何口令**，且**只回显我们自己刚生成的口令**：
   别人配的口令（环境变量 / 本地文件）一个字都不回显，只说清它在哪。

全部用例只读/只写临时目录，不碰真的 `data/db_keys.local.json`、不起服务、不联网。
"""
from __future__ import annotations

import json

import pytest

import launcher
from src import config


@pytest.fixture()
def local_keys(tmp_path, monkeypatch):
    """把密钥文件指到临时目录，并把鉴权状态摆成"开了、但没密钥"的现场。"""
    p = tmp_path / "db_keys.local.json"
    monkeypatch.setattr(config, "DB_KEYS_LOCAL_PATH", p)
    monkeypatch.setattr(config, "JWT_SECRET", "")
    monkeypatch.setattr(config, "AUTH_ENABLED", True)
    monkeypatch.setattr(config, "SEED_ADMIN_PASSWORD", "")
    monkeypatch.setattr(config, "SEED_ADMIN_USER", "admin")
    monkeypatch.setattr(launcher, "_seed_admin_exists", lambda: False)
    monkeypatch.delenv("SEED_ADMIN_PASSWORD", raising=False)
    return p


def test_no_auth_flag_skips_secret_entirely(local_keys):
    """`--no-auth` 是单机免登录形态：连密钥都不该生成。"""
    ok, note, hints = launcher.ensure_local_auth(no_auth=True, write=True)
    assert ok and hints == []
    assert not local_keys.exists(), "免登录形态不该落任何密钥文件"
    assert "免登录" in note


def test_check_is_read_only_but_says_it_will_generate(local_keys):
    """`--check` 承诺"只做预检不动服务"，因此也**不该落盘**；但必须说清启动时会生成。"""
    ok, note, hints = launcher.ensure_local_auth(no_auth=False, write=False)
    assert ok
    assert not local_keys.exists(), "--check 是只读预检"
    assert "自动生成" in note, "要让人知道：不改任何配置直接启动也能起来"
    assert any("现生成" in h for h in hints), "口令也会现生成，得先讲清楚"


def test_write_generates_secret_and_seed_password(local_keys):
    ok, note, hints = launcher.ensure_local_auth(no_auth=False, write=True)
    assert ok
    data = json.loads(local_keys.read_text(encoding="utf-8"))
    assert len(data["jwt_secret"]) >= 32, "密钥要够长（token_urlsafe(48) 级别）"
    pwd = data["seed_admin_password"]
    assert len(pwd) >= config.AUTH_MIN_PASSWORD_LEN, "现生成的口令也要过最小长度校验"
    assert any(pwd in h for h in hints), "打印出来的口令必须就是落盘的那个，否则登不进去"


def test_generated_credentials_differ_each_time(local_keys, tmp_path, monkeypatch):
    """写死/复用同一口令等于源码里的默认凭据 —— 每台机器、每次生成都必须不同。"""
    launcher.ensure_local_auth(no_auth=False, write=True)
    first = json.loads(local_keys.read_text(encoding="utf-8"))
    second_path = tmp_path / "second.json"
    monkeypatch.setattr(config, "DB_KEYS_LOCAL_PATH", second_path)
    monkeypatch.setattr(config, "JWT_SECRET", "")
    monkeypatch.setattr(config, "SEED_ADMIN_PASSWORD", "")
    launcher.ensure_local_auth(no_auth=False, write=True)
    second = json.loads(second_path.read_text(encoding="utf-8"))
    assert first["jwt_secret"] != second["jwt_secret"]
    assert first["seed_admin_password"] != second["seed_admin_password"]


def test_existing_keys_are_merged_not_overwritten(local_keys):
    """读-改-写：文件里已有 mysql_* / backend，覆盖掉等于把用户的库连接弄丢。"""
    local_keys.write_text(json.dumps({"backend": "sqlite", "mysql_host": "h"}),
                          encoding="utf-8")
    launcher.ensure_local_auth(no_auth=False, write=True)
    data = json.loads(local_keys.read_text(encoding="utf-8"))
    assert data["backend"] == "sqlite" and data["mysql_host"] == "h"


def test_configured_secret_is_left_alone_and_never_echoed(local_keys, monkeypatch):
    """已经配了密钥就不该再生成；密钥本身也绝不能被回显到终端/日志里。"""
    secret = "x" * 40
    monkeypatch.setattr(config, "JWT_SECRET", secret)
    ok, note, hints = launcher.ensure_local_auth(no_auth=False, write=True)
    assert ok and not local_keys.exists()
    assert "已启用" in note
    assert not any(secret in h for h in hints), "密钥不得回显"


def test_configured_seed_password_is_not_regenerated_nor_echoed(local_keys, monkeypatch):
    """运维显式配了口令就别动它（账号可能已用它建出来了），也不许回显。"""
    monkeypatch.setattr(config, "SEED_ADMIN_PASSWORD", "configured-pw-123")
    ok, _, hints = launcher.ensure_local_auth(no_auth=False, write=True)
    data = json.loads(local_keys.read_text(encoding="utf-8"))
    assert "jwt_secret" in data and "seed_admin_password" not in data
    assert ok
    assert not any("configured-pw-123" in h for h in hints), "不是我们生成的口令，一个字都不回显"
    assert any("seed_admin_password" in h for h in hints), "但要告诉人它在哪"


def test_existing_seed_admin_is_not_given_a_new_password(local_keys, monkeypatch):
    """账号已存在时口令不会变（ensure_seed_admin 幂等），那就不能打印一个登不进去的口令。"""
    monkeypatch.setattr(launcher, "_seed_admin_exists", lambda: True)
    ok, _, hints = launcher.ensure_local_auth(no_auth=False, write=True)
    pwd = json.loads(local_keys.read_text(encoding="utf-8"))["seed_admin_password"]
    assert ok
    assert not any(pwd in h for h in hints), "账号已存在，这个新口令根本没用上，不能打印"


def test_check_suggestion_reproduces_the_same_shape(capsys, local_keys):
    """预检末尾的建议命令要能**原样复现**本次形态：漏了 `--no-auth` 会让人以为还得配密钥。"""
    args = launcher.build_parser().parse_args(["--check", "--no-auth"])
    assert launcher._report_check(args, []) == 0
    out = capsys.readouterr().out
    assert "--no-auth" in out, "建议命令与本次预检形态不一致"
    assert "免登录" in out, "免登录形态要在预检里明说"


def test_docker_precheck_reports_missing_secret_instead_of_passing(monkeypatch):
    """容器形态的预检必须能表达"配了鉴权却没密钥"—— 否则 `--check` 的"通过"是假的。"""
    monkeypatch.delenv("FA_JWT_SECRET", raising=False)
    monkeypatch.setattr(launcher.shutil, "which", lambda _: "docker")
    monkeypatch.setattr(launcher, "_env_file_has", lambda _: False)
    problems = launcher.run_precheck(docker=True, no_auth=False)
    assert any("FA_JWT_SECRET" in p for p in problems)
    # --no-auth 时不该再拦：容器以免登录形态跑，没有密钥也能起
    assert launcher.run_precheck(docker=True, no_auth=True) == []
