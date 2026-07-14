# back_end_iraqi — FastAPI على RunPod

باكند FastAPI جاهز للعمل محلياً وعلى RunPod بقالب:
`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` (PyTorch 2.8.0 + CUDA 12.8.1 + Ubuntu 24.04)

ثلاث ميزات، كل وحدة براوترها ونصوصها الخاصة تحت `app/features/`:

| الميزة | الفولدر | الوصف |
|---|---|---|
| وكيل مبيعات | `app/features/sales/` | يقنع العميل بالشراء، يقترح منتجاً إضافياً، ويثبّت الطلب تلقائياً كـ JSON عند الجهوزية |
| دعم عملاء | `app/features/support/` | تتبع حالة الطلب برقم الطلب أو الهاتف (+ بحث ويب عام) |
| إنشاء طلب متعدد الوسائط | `app/features/order_intake/` | نص أو صوت أو صورة → JSON طلب مباشرة |

الموديل، RAG، الجلسات، كتالوج المنتجات، وصيغة الطلب مشتركة بجذر `app/` (تفصيل الملفات أدناه).

## التشغيل محلياً (Windows)

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

ثم افتح: http://localhost:8000/docs

> ملاحظة: torch/vllm غير مثبتين بالبيئة المحلية (حجمهم كبير وموجودين مسبقاً بصورة RunPod).
> `/gpu` سترجع `cuda: false`، وكل نقاط `/sales/*` و`/support/*` ترجع "[وضع محلي بدون GPU]" بدل توليد حقيقي — يكفي لاختبار الـ API نفسها.

## الرفع على RunPod — طريقتان

> دليل تفصيلي كامل خطوة بخطوة (مع حل المشاكل الشائعة) في [RUNPOD_DEPLOY.md](RUNPOD_DEPLOY.md).

### الطريقة 1: Pod مباشر بالقالب الجاهز (الأسرع)

1. أنشئ Pod من قالب **RunPod PyTorch 2.8.0** (الصورة أعلاه).
2. في إعدادات القالب أضف `8000` إلى **Expose HTTP Ports**.
3. انسخ المشروع للـ Pod (عبر Jupyter/SSH أو git clone) إلى `/workspace/back_end_iraqi`.
4. انسخ `.env.example` إلى `.env` واملأ `HF_TOKEN` (إلزامي) وبقية المفاتيح الاختيارية — أو أضفها كـ Environment Variables بإعدادات الـ Pod مباشرة.
5. شغّل:
   ```bash
   cd /workspace/back_end_iraqi && bash start.sh
   ```
6. الرابط يكون بالشكل: `https://<POD_ID>-8000.proxy.runpod.net`

### الطريقة 2: صورة Docker مخصصة (قالب خاص بك)

```bash
docker build -t <username>/back-end-iraqi:latest .
docker push <username>/back-end-iraqi:latest
```

ثم في RunPod أنشئ **Template** جديد:
- **Container Image**: `<username>/back-end-iraqi:latest`
- **Expose HTTP Ports**: `8000`
- أضف نفس متغيرات `.env.example` كـ Environment Variables بالـ Template.

> يتطلب البناء والدفع Docker مثبتاً وحساب Docker Hub (أو أي registry).

## نقاط الـ API

> توثيق كامل لكل نقطة (أمثلة طلبات/استجابات، شكل بث SSE، أخطاء) في [API.md](API.md).

| النقطة | الوصف |
|---|---|
| `GET /health` | فحص الصحة |
| `GET /gpu` | معلومات GPU/CUDA وحالة محرك vLLM |
| `POST /sales/chat` | وكيل مبيعات — رد كامل، يرجع `order` مملوءاً تلقائياً عند تثبيت الطلب |
| `POST /sales/chat/stream` | وكيل مبيعات — بث SSE، حدث `done` النهائي يحمل `order` |
| `POST /support/chat` | دعم عملاء — تتبع طلب برقم الطلب/الهاتف، أو سؤال عام (أداة بحث ويب) |
| `POST /orders/create` | إنشاء طلب من `text` أو `audio` (multipart) — يرجع JSON طلب مباشرة بدون محادثة |
| `GET /docs` | واجهة Swagger التفاعلية |
| `GET /test` | لوحة اختبار API تفاعلية (HTML/CSS/JS ثابتة، بدون تبعيات) — انظر [RUNPOD_DEPLOY.md](RUNPOD_DEPLOY.md#لوحة-اختبار-api-test-console) |

جسم الطلب لـ `/sales/chat`, `/sales/chat/stream`, `/support/chat`:

```json
{"message": "شنو معنى شلونك؟", "session_id": "اختياري لاستمرار نفس المحادثة"}
```

`/orders/create` مختلفة (multipart/form-data) — مدخل واحد بس من الثلاثة:

```bash
curl -F "text=أريد لابتوب لينوفو وحبة ماوس" http://localhost:8000/orders/create
curl -F "audio=@order.wav" http://localhost:8000/orders/create
```

## معمارية الميزات

كل ميزة مستقلة بفولدرها: `router.py` (نقاط الـ API) + `prompts.py` (system prompt خاص بها) + أي منطق إضافي (`service.py`, `client.py`...). البنية المشتركة:

| الملف | الدور |
|---|---|
| `app/config.py` | إعدادات مشتركة عبر متغيرات بيئة (موديل، RAG، أدوات) — **بدون أي سر مكتوب بالكود** |
| `app/engine.py` | غلاف vLLM: `render_prompt()` يبني نص البرومبت من chat template الموديل الفعلي، `generate_stream/full()` مع دعم `stop`/`guided_json`/`result_holder` |
| `app/tool_loop.py` | حلقة استدعاء أدوات عامة (`[TOOL_CALL]{...}[/TOOL_CALL]`) — مستخدمة حالياً من `support`، قابلة للربط بأي ميزة أخرى |
| `app/context_blocks.py` | صياغة نتائج RAG (لهجة/منتجات) كمقاطع نصية تُدمَج بأي system prompt |
| `app/sessions.py` | ذاكرة محادثة بالذاكرة (in-memory)، مفاتيحها مسبوقة باسم الميزة (`sales:...`, `support:...`) |
| `app/rag/` | بحث BM25 محلي لمصطلحات اللهجة العراقية (منسوخ من `iraqi_words_finetuning/rag`) |
| `app/products.py` | كتالوج المنتجات: `ProductRepository` (واجهة) + `StaticProductRepository` (JSON محلي حالياً — استبدلها بقاعدتك) |
| `app/order_schema.py` | `OrderExtraction` (خام من الموديل) / `OrderConfirmation` (بعد حساب الأسعار من الكتالوج) / `parse_order_extraction()` |
| `app/order_gateway.py` | بوابة نظام إدارة الطلبات الخارجي: **إخراج** (`OrderStatusProvider` — تتبع حالة، تستخدمه `support`) و**إدخال** (`OrderSubmitter` — تثبيت طلب جديد، تستخدمه `sales`/`order_intake` بعد حساب الأسعار) |
| `app/hf_utils.py` | `resolve_lora_path()` — تنزيل محوّل LoRA من Hugging Face Hub إن كان معرّف مستودع؛ مشتركة بين `engine.py` (vLLM) و`order_intake/vision.py` (transformers خام) |
| `app/tools/web_search.py` | أداة بحث ويب عامة عبر DuckDuckGo (`ddgs`) — بدون أي API key |

## نقاط توصيل مؤجَّلة (Mock الآن، استبدلها لاحقاً)

كل مصدر بيانات خارجي مفصول لواجهة **إخراج** (نستعلم منها) وواجهة **إدخال** (نرسل لها) — بدون مصادقة حسب توضيحك:

- **كتالوج المنتجات** (`app/products.py`) — إخراج فقط حالياً: `StaticProductRepository` فوق `app/data/products.json` (بيانات تجريبية). لربط قاعدة بياناتك الحقيقية، أنشئ صنفاً يطبّق `ProductRepository` (`search()`/`get_by_id()`) وبدّل السطر الأخير `product_repository = ...` — بدون تغيير أي راوتر.
- **نظام إدارة الطلبات** (`app/order_gateway.py`):
  - **إخراج** — `OrderStatusProvider.get_by_order_id()`/`.search_by_phone()`: حالياً `MockOrderStatusProvider` ببيانات تجريبية ثابتة. تستخدمه ميزة `support` لتتبع الطلبات.
  - **إدخال** — `OrderSubmitter.submit()`: حالياً `MockOrderSubmitter` يحتفظ بالطلبات بالذاكرة فقط. تستدعيه `sales/service.py` تلقائياً بعد كل طلب مؤكَّد لإرساله للنظام الخارجي.
  - لربط API حقيقي: أعطني رابط كل عملية (استعلام بمعرّف الطلب، بحث برقم الهاتف، تثبيت طلب جديد) وشكل الاستجابة، وأطبّق صنفين (`HttpOrderStatusProvider`, `HttpOrderSubmitter`) وأبدّل السطرين الأخيرين — بدون تغيير أي راوتر.
- **وصف الصورة** (`app/features/order_intake/vision.py`): مفعّلة عبر **نفس محرك vLLM** المستخدَم بباقي الميزات (Gemma 4 يدعم صور أصلاً عبر `multi_modal_data` — انظر `app/engine.py: render_multimodal_prompt`/`generate_full`) — **ماكو نسخة ثانية من الموديل ولا استهلاك VRAM إضافي**. محلياً بدون GPU ترجع `501` واضحة.

## محرك الاستدلال — vLLM

مبني حول **vLLM** (`app/engine.py`) لتحقيق زمن استجابة قريب من 1–1.5 ثانية:

- **Continuous Batching** و**PagedAttention**: مدمجتان تلقائياً في vLLM.
- **FlashAttention**: يختارها vLLM تلقائياً حسب العتاد إن كانت مدعومة.
- **Prefix Caching**: مفعّلة (`enable_prefix_caching`) لإعادة استخدام الـ KV cache لجزء الـ system prompt/سياق RAG المتكرر بين الطلبات.
- **Streaming**: نقاط `*/stream` تبث الفروقات النصية أولاً بأول.
- **RAG**: `app/rag/` (لهجة) + `app/products.py` (منتجات) يقلّصان السياق المُمرَّر للموديل بدل حقن البيانات كاملة.
- **قالب محادثة حقيقي**: `LLMEngine.render_prompt()` يستخدم `tokenizer.apply_chat_template()` للموديل المحمَّل فعلياً (Gemma أو أي موديل آخر) بدل قالب ثابت مكتوب يدوياً — يبقى صحيحاً مهما تغيّر `MODEL_NAME`.
- **FastAPI غير متزامن (async)**: كل نقاط الميزات `async def` وتستهلك مولّد vLLM غير المتزامن دون حجب الحلقة.

vLLM يحتاج GPU/Linux ولا يعمل محلياً على Windows؛ محلياً (`llm_engine.ready == False`) ترجع كل الميزات تلقائياً لوضع fallback (بدون توليد نموذج) حتى يشتغل الكود فعلياً على RunPod. التثبيت الفعلي في [requirements-gpu.txt](requirements-gpu.txt) ويحدث تلقائياً ضمن `start.sh` والـ Dockerfile.

### آلية "الوكيل يقرر" و"استدعاء الأدوات"

بدل الاعتماد على tool-calling الأصلي لـ vLLM (غير مؤكّد الدعم لموديل حديث جداً مثل Gemma 4)، نستخدم نمطاً نصياً بسيطاً:

- **[ORDER_READY]** (`app/features/sales/prompts.py`): الوكيل يختم رده بهذا السطر متى ما قرر إن العميل جاهز للشراء. يُمرَّر كـ `stop` لـ vLLM فما يوصل للعميل، ونتحقق منه عبر `stop_reason` (`app/engine.py`) بدل البحث بالنص.
- **[TOOL_CALL]{...}[/TOOL_CALL]** (`app/tool_loop.py`): نفس الفكرة معمَّمة لأي أداة (تتبع طلب، بحث ويب) — الموديل يطلب الأداة بنص محدد، الخادم ينفّذها ويعيد التوليد بجولة إضافية.

## الإعداد (متغيرات بيئة — انسخ [.env.example](.env.example) إلى `.env`)

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `MODEL_NAME` | `google/gemma-4-E4B-it` | الموديل الأساسي (يُنزَّل تلقائياً من HF Hub) |
| `LORA_PATH` | `ameer4wisam/gemma-iraqi-finetune` | مسار محلي أو معرّف مستودع HF لمحوّل LoRA (يُنزَّل تلقائياً إذا كان معرّف مستودع) |
| `LORA_RANK` | `16` | يجب أن يطابق قيمة `r` الفعلية في `adapter_config.json` على المستودع، وإلا يُتجاهل المحوّل بصمت |
| `HF_TOKEN` | (فارغ) | **مطلوب** — Gemma موديل بوابة (gated) ومستودع المحوّل خاص، بدون توكن صحيح يفشل التنزيل بخطأ 401/403 |
| `DTYPE` | `auto` | `auto` / `float16` / `bfloat16` |
| `QUANTIZATION` | (فارغ) | `bitsandbytes` لتحميل INT8، أو اتركه فارغاً لـ FP16/BF16 |
| `MAX_NUM_SEQS` | `32` | عرض الـ Continuous Batching |
| `MAX_MODEL_LEN` | `4096` | أقصى طول سياق |
| `RAG_TOP_K` | `5` | عدد وثائق RAG المسترجَعة لكل سؤال |
| `WHISPER_MODEL` | `ayoubkirouane/whisper-small-ar` | موديل تحويل الصوت لنص العربي (`app/features/order_intake/transcribe.py`) |

**تحذير أمني**: لا تكتب أي قيمة من الجدول أعلاه مباشرة بأي ملف `.py` — فقط عبر `.env` (مستثنى من git) أو Environment Variables بإعدادات RunPod. `app/config.py` يقرأها تلقائياً.

## تنزيل الموديل والمحوّل تلقائياً

كل شي يتنزّل وحده عند أول إقلاع، بدون أي أمر يدوي:

- **المكتبات**: `start.sh` والـ `Dockerfile` يشغّلون `pip install -r requirements.txt -r requirements-gpu.txt` تلقائياً (والـ`Dockerfile`/`start.sh` يثبّتان `ffmpeg` أيضاً، لازم لتحويل الصوت لنص).
- **الموديل الأساسي** (`MODEL_NAME`): `AsyncLLMEngine` ينزّله من Hugging Face Hub أول مرة يشتغل فيها ([app/engine.py](app/engine.py))، ويُخزَّن بذاكرة التخزين المؤقت (`~/.cache/huggingface` أو `HF_HOME`) فيُعاد استخدامه بالتشغيلات اللاحقة على نفس الـ pod/volume بدون إعادة تنزيل.
- **محوّل LoRA** (`LORA_PATH`): إذا كانت القيمة معرّف مستودع HF بدل مسار محلي، يُنزَّل تلقائياً عبر `huggingface_hub.snapshot_download` في `LLMEngine.start`.
- **موديل تحويل الصوت** (`WHISPER_MODEL`): ينزّله `transformers.pipeline` تلقائياً أول استخدام لـ `/orders/create` بصوت.

**خطوة لازمة قبل أول تشغيل — إعداد `HF_TOKEN`:**

1. اقبل ترخيص Gemma على حسابك في Hugging Face (صفحة الموديل → Agree and access repository).
2. تأكد أن نفس الحساب (أو حساب له صلاحية وصول) يقدر يفتح مستودع `ameer4wisam/gemma-iraqi-finetune` إذا كان خاصاً.
3. ولّد Access Token من https://huggingface.co/settings/tokens (صلاحية Read تكفي).
4. أضفه بـ `.env` محلياً أو كمتغير بيئة `HF_TOKEN` بإعدادات الـ Pod/Template على RunPod.

بدون هذا التوكن، أول تشغيل يفشل بخطأ 401/403 عند محاولة تحميل الموديل أو المحوّل.
