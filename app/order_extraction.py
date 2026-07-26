# -*- coding: utf-8 -*-
"""منطق استخراج الطلب المشترك بين مصادر الطلب كلها.

مصدرا الطلب اليوم:
  - /orders/create  — رسالة/صوت/صورة خام (app/features/order_intake)
  - /sales/chat     — محادثة مبيعات كاملة (app/features/sales)

كلاهما يمر من هنا حتى يطلع الطلب بمخطط plane.md نفسه ويأخذ نفس تصحيح
الموقع الحتمي. كان استخراج المبيعات يستعمل مخططاً مختلفاً
(customer_name/items) فيصل للعميل شكل JSON ثاني حسب الطريق اللي جاء منه.
"""

from typing import Optional

from app.order_schema import PlaneOrderExtraction
from app.rag import canonical_state, search_locations, state_for_district


def correct_location(plane: PlaneOrderExtraction) -> PlaneOrderExtraction:
    """تصحيح حتمي للموقع بعد الاستخراج، من قاعدة بيانات شركة التوصيل —
    الهدف أن يخرج city دائماً باسم محافظة رسمي من states.xlsx وdistrict
    باسم منطقة رسمي من districts.xlsx متى ما أمكن:

    1. إذا المنطقة المستخرجة معروفة وتتبع محافظة واحدة فقط → محافظتها هي
       city مهما خمّن الموديل (نفس قاعدة plane.md: كلمة العنوان تُصدَّق)،
       وdistrict يُوحَّد على الاسم الرسمي بالقاعدة.
    2. وإلا نوحّد إملاء city المستخرجة على الاسم الرسمي بقاعدة البيانات
       (بصره → البصرة، حله → بابل الحلة...)؛ وإذا ما طابقت أي محافظة
       نبقيها كما وردت.
    """
    by_district = state_for_district(plane.district)
    if by_district is None and not plane.district and plane.address:
        # المنطقة فارغة لكن ربما العنوان الحر يحتوي اسم منطقة معروفة —
        # نمسحه ونعتمد أول مطابقة حرفية غير غامضة (محافظة واحدة فقط)
        for hit in search_locations(plane.address, top_k=3):
            if hit["district"] and hit["exact"] and len(hit["candidates"]) == 1:
                by_district = {
                    "code": hit["state_code"],
                    "name": hit["state_name"],
                    "district": hit["district"],
                }
                break
    if by_district:
        plane.city = by_district["name"]
        plane.district = by_district["district"]
        return plane
    state = canonical_state(plane.city)
    if state:
        plane.city = state["name"]
    return plane


def state_code_for(city: Optional[str]) -> Optional[str]:
    """كود المحافظة بنظام شركة التوصيل (BGD...) من اسمها."""
    state = canonical_state(city or "")
    return state["code"] if state else None
