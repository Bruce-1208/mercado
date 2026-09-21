"""Install the offline translation models required by Mercado Libre publishing."""

from __future__ import annotations

import sys


REQUIRED_PAIRS = (
    ("es", "pt"),
    ("pt", "es"),
    # Mercado's CBT category predictor explicitly requires an English title.
    # These models keep category selection deterministic and offline.
    ("es", "en"),
    ("pt", "en"),
)


def _pair_label(source: str, target: str) -> str:
    return f"{source}→{target}"


def main() -> int:
    try:
        from argostranslate import package, translate
    except ImportError:
        print(
            "缺少 Argos Translate，请先执行："
            "python3 -m pip install -r bit/requirements-server.txt",
            file=sys.stderr,
        )
        return 1

    print("正在更新 Argos 官方模型索引……")
    package.update_package_index()
    available = list(package.get_available_packages())
    installed_pairs = {
        (str(item.from_code), str(item.to_code))
        for item in package.get_installed_packages()
        if getattr(item, "type", "translate") == "translate"
    }

    for source, target in REQUIRED_PAIRS:
        if (source, target) in installed_pairs:
            print(f"已安装 {_pair_label(source, target)}")
            continue
        model = next(
            (
                item
                for item in available
                if item.from_code == source and item.to_code == target
            ),
            None,
        )
        if model is None:
            print(
                f"官方模型索引中没有 {_pair_label(source, target)}",
                file=sys.stderr,
            )
            return 1
        print(f"正在下载并安装 {_pair_label(source, target)}……")
        package.install_from_path(model.download())

    translate.get_installed_languages.cache_clear()
    checks = (
        ("Hola, tenemos stock.", "es", "pt"),
        ("Olá, temos estoque.", "pt", "es"),
        ("Pulsera de cuarzo natural.", "es", "en"),
        ("Pulseira de quartzo natural.", "pt", "en"),
    )
    for text, source, target in checks:
        result = str(translate.translate(text, source, target) or "").strip()
        if not result:
            print(f"模型 {_pair_label(source, target)} 自检失败", file=sys.stderr)
            return 1
        print(f"自检 {_pair_label(source, target)}: {result}")

    print("本地翻译模型安装完成；上架翻译无需 API Key，也不会调用 DeepSeek。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
