# التوثيق

| المجلد / الملف | المحتوى |
|---|---|
| [architecture.md](architecture.md) | معمارية المشروع: الوحدات المشتركة، محرك vLLM، باك اند السستم، الإعدادات، التنزيل التلقائي |
| **api/** | |
| [api/overview.md](api/overview.md) | الرابط الأساسي، المصادقة، أنواع الأجسام، نقاط الفحص، ملخص كل النقاط |
| [api/orders.md](api/orders.md) | `POST /orders/create` وصيغة `OrderConfirmation` وأمثلة Java |
| [api/sales.md](api/sales.md) | المبيعات OpenAI-compatible (`/sales/*`) وأدواتها |
| [api/openai-compat.md](api/openai-compat.md) | `/v1/chat/completions` لـ jbot وأدوات الشحن |
| [api/voice-followup.md](api/voice-followup.md) | المتابعة الصوتية ومكالمة تأجيل التسليم ("صباح") |
| [api/district-correction.md](api/district-correction.md) | تصحيح مناطق شركات التوصيل والذاكرة والنموذج المدرَّب |
| **postman/** | |
| [postman/README.md](postman/README.md) | دليل اختبار الـ API على Postman |
| [postman/collection.json](postman/collection.json) | مجموعة Postman لكل النقاط |
| [postman/district-correction.json](postman/district-correction.json) | مجموعة Postman لتصحيح المناطق |
| **deploy/** | |
| [deploy/runpod.md](deploy/runpod.md) | خطوات الرفع على RunPod وحل المشاكل |
| **training/** | |
| [training/data-tips.md](training/data-tips.md) | أنماط ضعف الموديل ومعالجتها ببيانات التدريب |
| [training/data-prompt.md](training/data-prompt.md) | برومبت توليد بيانات التدريب |
| [training/gaps.md](training/gaps.md) | نواقص بيانات التدريب وخطة التخلي عن الدروع |
| **reference/** | |
| [reference/jenni-stores-schema.md](reference/jenni-stores-schema.md) | مخطط بيانات متاجر JENNI (مرجع حقول المتابعة الصوتية) |
| [reference/district-correction-import-report.json](reference/district-correction-import-report.json) | تقرير استيراد كتالوج المناطق |

تدريب reranker تصحيح المناطق موثّق بـ [../training/README.md](../training/README.md).
