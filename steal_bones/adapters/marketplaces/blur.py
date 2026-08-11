"""
Blur — раздел 3.1 ТЗ.

ЧЕСТНО: у Blur нет официального публичного API для сторонних разработчиков
(в отличие от Magic Eden/OpenSea/Rarible). Единственные встречающиеся пути —
неофициальные реверс-инжиниринг обёртки (нарушают ToS площадки, поэтому
сюда не закладываются, см. правило "без обхода антибот-защиты" в разделе 7
ТЗ) либо агрегаторы вроде Bitquery/Reservoir/SimpleHash, которые уже
декодируют сделки Blur через собственный официальный + бесплатный ключ.

Этот адаптер не делает "вид", что бьёт в несуществующий официальный
эндпоинт Blur — вместо этого явно говорит, что нужно подключить агрегатор.
"""

from __future__ import annotations

from adapters.marketplaces.base import ActivityRecord, AdapterError, MarketplaceAdapter


class BlurAdapter(MarketplaceAdapter):
    name = "blur"
    requires_key = False  # у самого Blur ключа получить негде — см. docstring
    SUPPORTED_ASSET_TYPES = {"nft"}

    def fetch_activity(self, asset_type: str, target: str, limit: int = 100, target_wallets: int = 20) -> list[ActivityRecord]:
        # target_wallets: параметр интерфейса (динамическая глубина поиска, см. base.py) —
        # этот адаптер пагинацию не делает, поэтому просто игнорирует значение.
        raise AdapterError(
            "Blur не публикует официальный API для сторонних разработчиков. "
            "Чтобы получать её данные легально, подключите агрегатор (например Bitquery, "
            "бесплатная регистрация на ide.bitquery.io — см. приложение «Ключи API») и "
            "напишите отдельный адаптер поверх него, фильтруя по marketplace='blur'."
        )
