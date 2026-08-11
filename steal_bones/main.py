"""
Steal Bones — Flask-приложение (замена FastAPI из исходного ТЗ, см.
README.md "Отличия от исходного ТЗ": в среде без сетевого доступа для pip
install FastAPI поставить было нельзя, а Flask уже был в образе и сам ТЗ
называл его допустимой альтернативой в разделе 2).
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime

from flask import Flask, flash, jsonify, redirect, render_template, request, send_file, url_for

import pipeline
import progress
from adapters.registry import BALANCE_ADAPTERS, MARKETPLACE_ADAPTERS, NETWORKS, PLATFORMS_BY_ASSET_TYPE
import ast
from config import KEY_ENV_MAP, save_env_values, settings, config
from db.crud import list_wallets

def parse_target_list(raw_target):
    if isinstance(raw_target, list):
        return [str(t).strip() for t in raw_target if str(t).strip()]
    if not raw_target:
        return []

    raw_str = str(raw_target).strip()

    if raw_str.startswith("[") and raw_str.endswith("]"):
        try:
            parsed = ast.literal_eval(raw_str)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            try:
                parsed = json.loads(raw_str.replace("'", '"'))
                if isinstance(parsed, list):
                    return [str(x).strip() for x in parsed if str(x).strip()]
            except Exception:
                pass

    cleaned = raw_str.replace('\r', '\n')
    items = []
    for line in cleaned.split('\n'):
        for item in line.split(','):
            item_str = item.strip().strip("'\"[]")
            if item_str:
                items.append(item_str)
    return items
from db.models import init_db
from export.excel_export import export_to_excel
from rate_limit.guard import QuotaTracker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("steal_bones.main")

app = Flask(__name__, template_folder="web/templates", static_folder="web/static")
app.secret_key = "steal-bones-local-tool"

init_db(settings.db_path)

NETWORK_LABELS = {
    "solana": "Solana", "ethereum": "Ethereum", "tron": "Tron", "bnb": "BNB Chain",
    "base": "Base", "arbitrum": "Arbitrum", "polygon": "Polygon", "bitcoin": "Bitcoin",
    "avalanche": "Avalanche", "sui": "Sui",
}
NETWORK_UNITS = {
    "solana": "SOL", "ethereum": "ETH", "tron": "TRX", "bnb": "BNB",
    "base": "ETH", "arbitrum": "ETH", "polygon": "POL", "bitcoin": "BTC",
    "avalanche": "AVAX", "sui": "SUI",
}
PLATFORM_LABELS = {
    "magic_eden": "Magic Eden", "opensea": "OpenSea", "tensor": "Tensor",
    "blur": "Blur", "rarible": "Rarible", "looksrare": "LooksRare",
    "dexscreener": "DexScreener", "birdeye": "Birdeye",
}

PLATFORM_NETWORKS = {
    "magic_eden": ["solana"],
    "tensor": ["solana"],
    "blur": ["ethereum"],
    "looksrare": ["ethereum"],
    "opensea": ["ethereum", "base", "polygon", "arbitrum", "avalanche", "bnb"],
    "rarible": ["ethereum", "polygon", "base", "arbitrum", "bnb", "avalanche", "solana"],
    "dexscreener": ["solana"],
    "birdeye": ["solana", "ethereum", "bnb", "base", "arbitrum", "polygon", "avalanche", "sui"],
}


@app.route("/")
def index():
    return redirect(url_for("dashboard"))


@app.route("/dashboard")
def dashboard():
    return render_template(
        "dashboard.html", active_page="dashboard",
        networks=NETWORKS, network_labels=NETWORK_LABELS, network_units=NETWORK_UNITS,
        platforms_by_asset_type=PLATFORMS_BY_ASSET_TYPE, platform_labels=PLATFORM_LABELS,
        platform_networks=PLATFORM_NETWORKS,
    )


def _run_job_background(platform, asset_type, network, target, target_wallets):
    try:
        if platform in ("magic_eden", "opensea"):
            job_config = {
                "platform": platform,
                "asset_type": asset_type,
                "network": network,
                "target": target,
                "target_wallets": target_wallets,
            }
            pipeline.run_pipeline(job_config)
        else:
            pipeline.run_job(platform, asset_type, network, target, min_balance=0.0,
                              target_wallets=target_wallets, force_recheck=False)
    except Exception as exc:
        logger.exception("Фоновый job упал неожиданно")
        progress.fail(str(exc))


@app.route("/run-job", methods=["POST"])
def run_job_route():
    platform = request.form.get("platform", "")
    asset_type = request.form.get("asset_type", "nft")
    network = request.form.get("network", "solana")
    raw_target = request.form.get("target", "")
    target_list = parse_target_list(raw_target)

    seen_targets = set()
    targets = [t for t in target_list if not (t in seen_targets or seen_targets.add(t))]
    target = targets if len(targets) > 1 else (targets[0] if targets else "")

    try:
        target_wallets = int(request.form.get("target_wallets", "20"))
    except ValueError:
        target_wallets = 20
    target_wallets = max(1, min(target_wallets, 10000))

    allowed_networks = PLATFORM_NETWORKS.get(platform, NETWORKS)
    if allowed_networks and network not in allowed_networks:
        logger.warning("run-job: сеть '%s' не поддерживается площадкой '%s', исправляю на '%s'",
                        network, platform, allowed_networks[0])
        flash(f"{platform} не работает в сети «{network}» — использована {allowed_networks[0]} вместо неё.", "warn")
        network = allowed_networks[0]

    logger.info("run-job: platform=%s asset_type=%s network=%s target=%s target_wallets=%s (получено из формы)",
                platform, asset_type, network, target, target_wallets)

    if not target:
        flash("Укажите коллекцию/токен.", "warn")
        return redirect(url_for("dashboard"))

    thread = threading.Thread(
        target=_run_job_background,
        args=(platform, asset_type, network, target, target_wallets),
        daemon=True,
    )
    thread.start()

    return redirect(url_for("job_progress"))


@app.route("/job-progress")
def job_progress():
    if progress.snapshot()["status"] == "idle":
        flash("Сбор ещё не запускался — запустите его на Панели.", "warn")
        return redirect(url_for("dashboard"))
    return render_template("job_progress.html", active_page="dashboard")


@app.route("/api/job-status")
def api_job_status():
    return jsonify(progress.snapshot())


@app.route("/results")
def results():
    network_filter = request.args.get("network") or None
    collection_filter = request.args.get("collection") or None
    order_by = request.args.get("order_by", "last_seen")
    order_dir = request.args.get("order_dir", "DESC")
    only_skipped = request.args.get("view") == "skipped"

    # Полностью убираем фильтрацию по балансу (min_balance=None)
    rows = list_wallets(settings.db_path, network=network_filter, min_balance=None,
                         collection=collection_filter, order_by=order_by, order_dir=order_dir,
                         only_skipped=only_skipped)
    all_count = len(list_wallets(settings.db_path))
    wallets = []
    for r in rows:
        w = dict(r)
        try:
            w["extra_assets"] = json.loads(w["extra_assets"]) if w.get("extra_assets") else None
        except (TypeError, ValueError):
            w["extra_assets"] = None
        wallets.append(w)

    return render_template(
        "results.html", active_page="results",
        wallets=wallets, networks=NETWORKS, network_labels=NETWORK_LABELS,
        network_filter=network_filter or "", collection_filter=collection_filter or "",
        all_count=all_count, order_by=order_by, order_dir=order_dir, only_skipped=only_skipped,
    )


@app.route("/export")
def export_excel():
    network_filter = request.args.get("network") or None
    collection_filter = request.args.get("collection") or None
    only_skipped = request.args.get("view") == "skipped"

    # Полностью убираем фильтрацию по балансу (min_balance=None)
    rows = list_wallets(settings.db_path, network=network_filter, min_balance=None,
                         collection=collection_filter, only_skipped=only_skipped)
    suffix = "_skipped" if only_skipped else ""
    out_path = settings.export_dir / f"steal_bones_export{suffix}_{datetime.now():%Y%m%d_%H%M%S}.xlsx"
    export_to_excel(rows, out_path)

    return send_file(out_path, as_attachment=True, download_name=out_path.name)


@app.route("/api/check-collection")
def api_check_collection():
    platform = request.args.get("platform", "")
    target = request.args.get("target", "").strip()
    if not target:
        return {"status": "empty"}

    if platform not in ("magic_eden", "opensea"):
        return {"status": "unsupported"}

    adapter = MARKETPLACE_ADAPTERS.get(platform)
    try:
        info = adapter.check_collection_exists(target)
    except Exception as exc:
        logger.warning("Проверка коллекции %s (%s) не удалась: %s", target, platform, exc)
        return {"status": "error"}

    if info is None:
        return {"status": "not_found"}
    return {"status": "found", **info}


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    from db.crud import get_settings, update_settings

    if request.method == "POST":
        # Save Helius and OpenSea API keys directly to the database settings table (row id=1)
        helius_api_key = request.form.get("HELIUS_API_KEY", "").strip()
        opensea_api_key = request.form.get("OPENSEA_API_KEY", "").strip()
        update_settings(settings.db_path, helius_api_key, opensea_api_key)

        # Save all environmental values as fallback to .env (using form inputs)
        values = {env_name: request.form.get(env_name, "").strip() for env_name in KEY_ENV_MAP.values()}
        save_env_values(values)
        config.reload()

        flash("Ключи сохранены в БД и .env", "ok")
        return redirect(url_for("settings_page"))

    # Query DB keys with fallback
    db_keys = get_settings(settings.db_path)
    current_helius = db_keys.get("helius_api_key") or ""
    current_opensea = db_keys.get("opensea_api_key") or ""

    key_fields = [
        {"name": "ETHERSCAN_API_KEYS", "label": "Etherscan API V2 (6 EVM-сетей)",
         "value": ",".join(settings.etherscan_keys), "placeholder": "ключ1,ключ2", "rotation": True},
        {"name": "OPENSEA_API_KEY", "label": "OpenSea", "value": current_opensea, "placeholder": "", "rotation": False},
        {"name": "TENSOR_API_KEY", "label": "Tensor", "value": settings.tensor_key, "placeholder": "", "rotation": False},
        {"name": "RARIBLE_API_KEY", "label": "Rarible", "value": settings.rarible_key, "placeholder": "", "rotation": False},
        {"name": "LOOKSRARE_API_KEY", "label": "LooksRare", "value": settings.looksrare_key, "placeholder": "", "rotation": False},
        {"name": "HELIUS_API_KEY", "label": "Helius (доп. данные Solana)",
         "value": current_helius, "placeholder": "", "rotation": True},
        {"name": "TRONGRID_API_KEY", "label": "TronGrid", "value": settings.trongrid_key, "placeholder": "", "rotation": False},
        {"name": "XVERSE_API_KEY", "label": "Xverse (Bitcoin Ordinals/Runes)", "value": settings.xverse_key, "placeholder": "", "rotation": False},
        {"name": "BLOCKBERRY_API_KEY", "label": "Blockberry (Sui)", "value": settings.blockberry_key, "placeholder": "", "rotation": False},
        {"name": "BIRDEYE_API_KEY", "label": "Birdeye (мемкоины)", "value": settings.birdeye_key, "placeholder": "", "rotation": False},
    ]

    quota_status = []
    for name, adapter in MARKETPLACE_ADAPTERS.items():
        qt = QuotaTracker(source=name, key_label="default", daily_limit=adapter.default_daily_limit, db_path=settings.db_path)
        quota_status.append({"source": PLATFORM_LABELS.get(name, name), "used": qt.used_today(),
                              "limit": qt.daily_limit, "ratio": qt.usage_ratio()})

    return render_template(
        "settings.html", active_page="settings",
        key_fields=key_fields, quota_status=quota_status,
    )


@app.route("/api-keys-guide")
def api_keys_guide():
    return render_template("api_keys_guide.html", active_page="guide")


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
