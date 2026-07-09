# back_end_iraqi — FastAPI على RunPod

باكند FastAPI جاهز للعمل محلياً وعلى RunPod بقالب:
`runpod/pytorch:1.0.2-cu1281-torch280-ubuntu2404` (PyTorch 2.8.0 + CUDA 12.8.1 + Ubuntu 24.04)

## التشغيل محلياً (Windows)

```powershell
.venv\Scripts\Activate.ps1
uvicorn app.main:app --reload --port 8000
```

ثم افتح: http://localhost:8000/docs

> ملاحظة: torch غير مثبت في البيئة المحلية (حجمه كبير وموجود مسبقاً في صورة RunPod).
> نقطة `/gpu` سترجع `cuda: false` محلياً وهذا طبيعي.

## الرفع على RunPod — طريقتان

### الطريقة 1: Pod مباشر بالقالب الجاهز (الأسرع)

1. أنشئ Pod من قالب **RunPod PyTorch 2.8.0** (الصورة أعلاه).
2. في إعدادات القالب أضف `8000` إلى **Expose HTTP Ports**.
3. انسخ المشروع للـ Pod (عبر Jupyter/SSH أو git clone) إلى `/workspace/back_end_iraqi`.
4. شغّل:
   ```bash
   cd /workspace/back_end_iraqi && bash start.sh
   ```
5. الرابط يكون بالشكل: `https://<POD_ID>-8000.proxy.runpod.net`

### الطريقة 2: صورة Docker مخصصة (قالب خاص بك)

```bash
docker build -t <username>/back-end-iraqi:latest .
docker push <username>/back-end-iraqi:latest
```

ثم في RunPod أنشئ **Template** جديد:
- **Container Image**: `<username>/back-end-iraqi:latest`
- **Expose HTTP Ports**: `8000`

> يتطلب البناء والدفع Docker مثبتاً وحساب Docker Hub (أو أي registry).

## نقاط الـ API

| النقطة | الوصف |
|---|---|
| `GET /health` | فحص الصحة |
| `GET /gpu` | معلومات GPU وCUDA وحالة محرك vLLM (للتأكد على RunPod) |
| `POST /chat` | رد كامل (بدون بث) — RAG + توليد |
| `POST /chat/stream` | نفس الشيء لكن ببث Server-Sent Events (`data: {"delta": "..."}`) |
| `GET /docs` | واجهة Swagger التفاعلية |

جسم الطلب لـ `/chat` و`/chat/stream`:

```json
{"message": "شنو معنى شلونك؟", "session_id": "اختياري لاستمرار نفس المحادثة"}
```

## محرك الاستدلال — vLLM

الباك اند مبني حول **vLLM** (`app/engine.py`) لتحقيق زمن استجابة قريب من 1–1.5 ثانية:

- **Continuous Batching** و**PagedAttention**: مدمجتان تلقائياً في vLLM.
- **FlashAttention**: يختارها vLLM تلقائياً حسب العتاد إن كانت مدعومة.
- **Prefix Caching**: مفعّلة (`enable_prefix_caching`) لإعادة استخدام الـ KV cache لجزء الـ system prompt/سياق RAG المتكرر بين الطلبات.
- **Streaming**: `/chat/stream` يبث الفروقات النصية أولاً بأول.
- **RAG**: `app/rag/` (بحث BM25 محلي بدون مكتبات خارجية) يقلّص السياق المُمرَّر للموديل بدل حقن قاعدة البيانات كاملة — منسوخ من [iraqi_words_finetuning/rag](../../iraqi_words_finetuning/rag)، أعد نسخ `documents.jsonl` بعد أي تعديل على `word.json` هناك.
- **FastAPI غير متزامن (async)**: كل نقاط `/chat*` معرّفة بـ `async def` وتستهلك مولّد vLLM غير المتزامن دون حجب الحلقة.

vLLM يحتاج GPU/Linux ولا يعمل محلياً على Windows؛ محلياً (`llm_engine.ready == False`) يرجع الباك اند تلقائياً لوضع RAG-only (أقرب مطابقة من قاعدة المصطلحات بدون توليد) حتى تختبر بقية الـ API. التثبيت الفعلي لـ vLLM في [requirements-gpu.txt](requirements-gpu.txt) ويحدث تلقائياً ضمن `start.sh` والـ Dockerfile.

### الإعداد (متغيرات بيئة، اختيارية — انظر [app/config.py](app/config.py))

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

## تنزيل الموديل والمحوّل تلقائياً

كل شي يتنزّل وحده عند أول إقلاع، بدون أي أمر يدوي:

- **المكتبات**: `start.sh` والـ `Dockerfile` يشغّلون `pip install -r requirements.txt -r requirements-gpu.txt` تلقائياً.
- **الموديل الأساسي** (`MODEL_NAME`): `AsyncLLMEngine` ينزّله من Hugging Face Hub أول مرة يشتغل فيها ([app/engine.py](app/engine.py))، ويُخزَّن بذاكرة التخزين المؤقت (`~/.cache/huggingface` أو `HF_HOME`) فيُعاد استخدامه بالتشغيلات اللاحقة على نفس الـ pod/volume بدون إعادة تنزيل.
- **محوّل LoRA** (`LORA_PATH`): إذا كانت القيمة معرّف مستودع HF (مثل `ameer4wisam/gemma-iraqi-finetune`) بدل مسار محلي، يُنزَّل تلقائياً عبر `huggingface_hub.snapshot_download` في `LLMEngine.start`. إذا كان مساراً محلياً موجوداً (مثل ناتج `fine_tuning/train.py` بعد نسخه للـ pod) يُستخدم مباشرة بدون تنزيل.

**خطوة لازمة قبل أول تشغيل — إعداد `HF_TOKEN`:**

1. اقبل ترخيص Gemma على حسابك في Hugging Face (صفحة الموديل → Agree and access repository).
2. تأكد أن نفس الحساب (أو حساب له صلاحية وصول) يقدر يفتح مستودع `ameer4wisam/gemma-iraqi-finetune` إذا كان خاصاً.
3. ولّد Access Token من https://huggingface.co/settings/tokens (صلاحية Read تكفي).
4. أضفه كمتغير بيئة `HF_TOKEN` بإعدادات الـ Pod/Template على RunPod (وليس بالكود مباشرة).

بدون هذا التوكن، أول تشغيل يفشل بخطأ 401/403 عند محاولة تحميل الموديل أو المحوّل.

## أين الموديل؟

محرّك التوليد الفعلي في [app/engine.py](app/engine.py) (`LLMEngine.start` يُستدعى من `lifespan` في [app/main.py](app/main.py)).
