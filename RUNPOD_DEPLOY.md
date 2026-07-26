# خطوات الرفع على RunPod

هذا الملف دليل تفصيلي خطوة بخطوة لرفع `back_end_iraqi` على RunPod. للتوثيق المرجعي لنقاط الـ API نفسها انظر [API.md](API.md)، وللوحة اختبار تفاعلية بالمتصفح انظر قسم [لوحة اختبار API](#لوحة-اختبار-api-test-console) بالأسفل.

اختر إحدى الطريقتين:

- **الطريقة 1 — Pod مباشر** (أسرع، مناسبة للتجربة والتطوير)
- **الطريقة 2 — صورة Docker مخصصة** (أفضل لبيئة إنتاج ثابتة وقابلة لإعادة الاستخدام)

---

## قبل البدء — تجهيزات إلزامية

### 1. توكن Hugging Face (`HF_TOKEN`)

Gemma موديل بوابة (gated) ومستودع محوّل اللهجة العراقية خاص، فلازم توكن صحيح قبل أي تشغيل:

1. افتح صفحة الموديل الأساس على Hugging Face: `google/gemma-4-12B-it` → اضغط **Agree and access repository**.
2. تأكد أن نفس الحساب (أو حساب له صلاحية وصول) يقدر يفتح مستودع الموديل المدموج `ameer4wisam/gemma-iraqi-finetune-v2`.
3. اذهب إلى https://huggingface.co/settings/tokens وولّد **Access Token** جديد (صلاحية **Read** تكفي للتشغيل؛ تحتاج **Write** فقط لو رح ترفع ملفات للمستودع).
4. احتفظ بالتوكن جانباً — رح تحتاجه بخطوة متغيرات البيئة أدناه.

بدون هذا التوكن، أول تشغيل يفشل بخطأ 401/403 عند تحميل الموديل أو المحوّل.

### 2. حساب RunPod

أنشئ حساب على https://runpod.io وأضف رصيداً كافياً (GPU بذاكرة كبيرة مطلوبة — انظر توصية الـ GPU أدناه).

### توصية اختيار GPU

الموديل المدموج `gemma-iraqi-finetune-v2` (Gemma 4 12B) يحتاج ~24GB VRAM لأوزانه بـ `bfloat16` + مساحة KV cache للطلبات المتزامنة — GPU بذاكرة **40GB+** موصى به. **A40 (48GB)** خيار ممتاز من ناحية السعر/الأداء: الأوزان + ~19GB KV cache تكفي لمئات الطلبات المتزامنة القصيرة عبر continuous batching.

---

## الطريقة 1: Pod مباشر بالقالب الجاهز

### الخطوة 1 — إنشاء الـ Pod

1. من لوحة RunPod، اضغط **Deploy** → **GPU Pod**.
2. الأفضل: قالب مخصص بصورة **`vllm/vllm-openai:gemma4-unified`** — نفس صورة [Dockerfile](Dockerfile) (خادم vLLM الرسمي بدعم Gemma 4). بديل: أي قالب PyTorch حديث — `start.sh` يثبّت vLLM nightly تلقائياً إذا كان ناقصاً (دعم Gemma 4 لم يصدر بعد بإصدار vLLM مستقر).
3. اختر GPU مناسب (انظر التوصية أعلاه).
4. تحت **Edit Template** (أو أثناء الإنشاء):
   - أضف `8000` إلى حقل **Expose HTTP Ports**.
   - تأكد أن حجم القرص (Disk / Container Disk) كافٍ (**50GB+** موصى به) لأن الموديل والمحوّل يُنزَّلان تلقائياً بأول تشغيل.
5. اضغط **Deploy**.

### الخطوة 2 — نسخ المشروع للـ Pod

بعد ما يصير الـ Pod بحالة **Running**، افتح **Connect** → **Start Web Terminal** (أو اتصل بـ SSH لو فعّلته):

```bash
cd /workspace
git clone https://github.com/ameer20042005/back_end_iraqi.git app
cd /workspace/app
```

> ملاحظة: بقية هذا الدليل يفترض المسار `/workspace/app`. لو استنسخت باسم ثاني، بدّله بكل الأوامر أدناه.

### الخطوة 3 — إعداد متغيرات البيئة

```bash
cd /workspace/app
cp .env.example .env
nano .env   # أو أي محرر متاح
```

أهم متغير لازم تضبطه:

```
HF_TOKEN=hf_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

باقي المتغيرات (`MODEL_NAME`, `VLLM_PORT`, `MAX_MODEL_LEN`, `GPU_MEMORY_UTILIZATION`...) لها قيم افتراضية معقولة — عدّلها فقط إذا لازم (تفصيل كامل بجدول [README.md](README.md#الإعداد-متغيرات-بيئة--انسخ-envexample-إلى-env)).

> **بديل**: بدل تعديل `.env`، تكدر تضيف نفس المتغيرات مباشرة من إعدادات الـ Pod (**Edit Pod** → **Environment Variables**) — تُقرأ تلقائياً بنفس الطريقة.

### الخطوة 4 — التشغيل (بـ tmux حتى يضل شغال بعد غلق الطرفية)

انسخ هذي الأوامر كما هي:

```bash
cd /workspace/app
export HF_HUB_ENABLE_HF_TRANSFER=0
tmux kill-session -t api 2>/dev/null
tmux new-session -d -s api 'bash start.sh > /tmp/api.log 2>&1'
```

خلص — السيرفر يشتغل بالخلفية ويضل شغال حتى لو غلقت الطرفية.

**ليش هيك مو `tmux new -s api` بعدين `bash start.sh`؟** لأن `new-session -d` تبدأ الجلسة **منفصلة أصلاً**، فما تحتاج تضغط `Ctrl+B` ثم `D` للخروج — وهذي الضغطة أكثر خطوة تنكسر بالعادة (لو انضغطت غلط تظهر كنص `^B^B` والسيرفر يموت مع الطرفية).

**ليش `HF_HUB_ENABLE_HF_TRANSFER=0`؟** بعض قوالب RunPod تضبط هذا المتغير على `1` بدون ما تكون حزمة `hf_transfer` مثبّتة، فيفشل **أي** تحميل من Hugging Face بخطأ:
```
ValueError: Fast download using 'hf_transfer' is enabled ... but 'hf_transfer' package is not available
```
تصفيره يرجّع التحميل العادي. (بديل: `pip install hf_transfer`.)

#### متابعة الإقلاع

**أول تشغيل ياخذ وقت أطول** (تحميل الموديل المدموج ~24GB من Hugging Face + تجهيز vLLM):

```bash
tail -f /tmp/api.log          # لوج FastAPI + خطوات start.sh
tail -f /tmp/vllm_boot.log    # لوج خادم vLLM نفسه (تحميل الأوزان)
```

اخرج من المتابعة بـ `Ctrl+C` — هذا يوقف `tail` فقط، ما يمس السيرفر داخل tmux.

الـ API يشتغل فوراً بوضع fallback ويتحول تلقائياً لوضع الموديل أول ما يجهز vLLM — راقب حتى تشوف:
```
✅ خادم vLLM جاهز على http://127.0.0.1:18001/v1
```

#### أوامر tmux اللي تحتاجها

| الأمر | الفايدة |
|---|---|
| `tmux ls` | يعرض الجلسات الشغالة (لازم تشوف `api`) |
| `tmux attach -t api` | تدخل الجلسة وتشوف اللوج حي |
| `Ctrl+B` ثم `D` | تطلع من الجلسة **بدون** ما توقف السيرفر (اضغطهن بالتتابع، مو سوية) |
| `tmux kill-session -t api` | توقف السيرفر نهائياً |

> ⚠️ لا تستخدم `Ctrl+C` أو `exit` وأنت داخل الجلسة — ينهون السيرفر.

#### تحقق إنه فعلاً نجا من غلق الطرفية

اغلق الطرفية، افتح وحدة جديدة، وشغّل:

```bash
tmux ls
curl -s http://127.0.0.1:8000/health
```

لو طلعت `api: 1 windows` ورد `{"status":"healthy"}` — تمام.

> **حدود tmux**: يحمي من غلق الطرفية فقط، **مو** من إيقاف الـ Pod أو إعادة تشغيله. لو أوقفت الـ Pod من لوحة RunPod تنفقد الجلسة ولازم تعيد أوامر الخطوة 4. للبقاء عبر إعادة التشغيل استخدم [الطريقة 2](#الطريقة-2-صورة-docker-مخصصة) (الحاوية تشغّل `start.sh` تلقائياً).

#### تحديث الكود لاحقاً

```bash
cd /workspace/app
git fetch origin && git checkout main && git reset --hard origin/main
tmux kill-session -t api 2>/dev/null
tmux new-session -d -s api 'bash start.sh > /tmp/api.log 2>&1'
```

> ⚠️ `git reset --hard` يمحي أي تعديل محلي بالـ Pod. لو عندك تعديلات تريد تحتفظ بيها، شغّل `git stash` قبله.

### الخطوة 5 — الوصول للسيرفر

الرابط العام يكون بالشكل:
```
https://<POD_ID>-8000.proxy.runpod.net
```

تلقى `<POD_ID>` بلوحة RunPod تحت تفاصيل الـ Pod، أو مباشرة بزر **Connect** → **HTTP Service [Port 8000]**.

تحقق من التشغيل:
```bash
curl https://<POD_ID>-8000.proxy.runpod.net/health
curl https://<POD_ID>-8000.proxy.runpod.net/gpu
```

`/gpu` يجب يرجع `"vllm_ready": true` بعد اكتمال تحميل الموديل.

---

## الطريقة 2: صورة Docker مخصصة

أفضل لو تريد بيئة ثابتة قابلة لإعادة النشر بدون خطوات يدوية (CI/CD، فرق عمل، إعادة تشغيل متكررة).

### الخطوة 1 — بناء الصورة محلياً

يتطلب Docker مثبَّت وحساب على Docker Hub (أو أي container registry آخر).

```bash
docker build -t <username>/back-end-iraqi:latest .
```

> ملاحظة: الصورة الأساسية (`vllm/vllm-openai:gemma4-unified`) كبيرة الحجم — البناء قد ياخذ وقتاً حسب سرعة الاتصال. على مضيف CUDA 12.9 استخدم الوسم `gemma4-unified-cu129`.

### الخطوة 2 — رفع الصورة

```bash
docker login
docker push <username>/back-end-iraqi:latest
```

### الخطوة 3 — إنشاء Template على RunPod

من لوحة RunPod: **Templates** → **New Template**:

| الحقل | القيمة |
|---|---|
| **Container Image** | `<username>/back-end-iraqi:latest` |
| **Container Disk** | 50GB+ (لتحميل الموديل) |
| **Expose HTTP Ports** | `8000` |
| **Environment Variables** | نفس متغيرات `.env.example` — خصوصاً `HF_TOKEN` |

### الخطوة 4 — نشر Pod من القالب

**Deploy** → اختر القالب اللي أنشأته → اختر GPU → **Deploy**.

الحاوية تُشغِّل تلقائياً `start.sh` (معرَّف بآخر سطر بـ [Dockerfile](Dockerfile)) — خادم vLLM على 18001 بالخلفية + FastAPI على 8000 — بدون أي أمر يدوي إضافي، وبدون حاجة لـ tmux (الحاوية نفسها تضل شغالة، وترجع تشتغل تلقائياً بعد إعادة تشغيل الـ Pod).

### الخطوة 5 — التحقق

نفس خطوة التحقق بالطريقة 1:
```bash
curl https://<POD_ID>-8000.proxy.runpod.net/health
curl https://<POD_ID>-8000.proxy.runpod.net/gpu
```

---

## لوحة اختبار API (Test Console)

بعد ما يشتغل السيرفر (محلياً أو على RunPod)، افتح بالمتصفح:

```
http://localhost:8000/test          (محلياً)
https://<POD_ID>-8000.proxy.runpod.net/test   (على RunPod)
```

صفحة HTML/CSS/JS ثابتة (بدون أي تبعيات خارجية أو مكتبات) مبنية داخل المشروع (`static/index.html`)، تسمح باختبار كل نقاط الـ API من المتصفح مباشرة بدون Postman أو `curl`:

- **حقل رابط السيرفر** أعلى الشريط الجانبي — معبّأ تلقائياً برابط الصفحة الحالية، وتكدر تبدّله لأي رابط ثاني (مفيد لو فتحت الصفحة محلياً بس تريد تختبر Pod شغال على RunPod).
- **مؤشر حالة الاتصال** — يفحص `/health` و`/gpu` ويوضح إذا الموديل شغال فعلاً (`vLLM`) أو بوضع fallback بدون GPU.
- **كل نقطة براوترها الخاص** بالقائمة الجانبية:
  - `GET /health`, `/gpu`, `/` — زر إرسال واحد يعرض الـ JSON مع تلوين وترتيب تلقائي.
  - `POST /sales/chat` — واجهة محادثة كاملة (فقاعات رسائل)، تحافظ على `session_id` تلقائياً بين الرسائل، وتعرض تفاصيل الطلب (`order`) كبطاقة منسّقة لما يتثبّت.
  - `POST /sales/chat/stream` — نفس الفكرة لكن تعرض النص أولاً بأول أثناء البث (SSE)، مع قياس زمن أول توكن والزمن الإجمالي.
  - `POST /support/chat` — محادثة دعم عملاء بنفس فكرة الجلسة المستمرة.
  - `POST /orders/create` — نموذج برفع ملف (صوت/صورة) أو حقل نص، حسب النوع المختار.
- **زر "عرض أمر curl المكافئ"** أسفل كل استجابة — يولّد أمر `curl` جاهز لنفس الطلب، مفيد للتوثيق أو الأتمتة لاحقاً.
- الصفحة تدعم **الوضع الداكن/الفاتح** (تلقائي حسب النظام، أو تبديل يدوي من الشريط الجانبي) وتُخزّن آخر رابط سيرفر ونقطة مفتوحة محلياً بالمتصفح (`localStorage`) لتوفير وقتك بالمرات القادمة.

> الصفحة تُقدَّم مباشرة من FastAPI نفسه (`GET /test` في [app/main.py](app/main.py)) فتفتح مباشرة بعد الرفع بدون أي إعداد إضافي أو استضافة منفصلة.

---

## مشاكل شائعة

| المشكلة | السبب المحتمل | الحل |
|---|---|---|
| **كل الردود صارت جملة وحدة ثابتة** (مثل «عفواً حبيبي، ما وصلتني زين») مهما كان السؤال — عربي أو إنجليزي | المُرمِّز (tokenizer) محمّل فاضي: `vocab_size=5` بدل 262144، فكل نص يتحول `<unk>` والموديل ما يشوف رسالة العميل أصلاً. يصير لو `tokenizer.json` ناقص من مستودع الموديل + نسخة `transformers` جديدة (5.x ما عاد يبني المفردات ضمنياً من `tokenizer_config.json` وحده) | افحص بالأمر أدناه؛ لو `vocab` مو 262144 لازم ترفع `tokenizer.json` لمستودع الموديل من الأساس (`google/gemma-4-12B-it`). النسخ مثبّتة بـ `requirements-gpu.txt` حتى ما تتكرر ترقية صامتة |
| فشل أي تحميل من Hugging Face بخطأ `hf_transfer` | قالب الـ Pod ضابط `HF_HUB_ENABLE_HF_TRANSFER=1` بدون الحزمة مثبّتة | `export HF_HUB_ENABLE_HF_TRANSFER=0` (أو `pip install hf_transfer`) — انظر الخطوة 4 |
| السيرفر يموت أول ما تغلق الطرفية | ما اشتغل داخل tmux فعلياً (شائع: `Ctrl+B` `D` انضغطت غلط فظهرت كنص `^B^B`) | استخدم `tmux new-session -d` بالخطوة 4 — تبدأ منفصلة أصلاً بلا أي اختصار |
| `git pull` يفشل بـ `no such ref was fetched` | الـ Pod على فرع محذوف من الريموت | `git fetch origin && git checkout main && git reset --hard origin/main` |
| `bash start.sh` يطلع Nginx/SSH/Jupyter و«Pod is ready to use» | شغّلت سكربت إقلاع RunPod مو سكربت المشروع (كنت بمسار غلط) | `cd /workspace/app` أولاً، بعدين `bash start.sh` |
| خطأ 401/403 عند التشغيل | `HF_TOKEN` مفقود أو غير صحيح، أو لم تقبل ترخيص Gemma | راجع قسم "قبل البدء" أعلاه |
| `/gpu` يرجع `vllm_ready: false` باستمرار | خادم vLLM لسا يحمّل الموديل (~24GB أول مرة)، أو فشل إقلاعه | راقب لوج الـ Pod؛ تأكد أن نسخة vLLM تدعم Gemma 4 (صورة `gemma4-unified` أو nightly — الإصدارات المستقرة الحالية لا تدعمه) |
| نفاد ذاكرة GPU (CUDA OOM) | GPU المختار صغير جداً، أو `GPU_MEMORY_UTILIZATION`/`MAX_MODEL_LEN` مرتفعة جداً | اختر GPU أكبر (40GB+)، أو قلّل `MAX_MODEL_LEN`/`GPU_MEMORY_UTILIZATION` في `.env` |
| الصفحة `/test` ما تتصل بالسيرفر (CORS) | نادراً — الـ CORS مفتوح للجميع افتراضياً بـ `app/main.py` | تأكد إن رابط السيرفر بحقل "رابط السيرفر" صحيح ويتضمن `https://` |
| أول طلب بطيء جداً | طبيعي — تحميل الموديل والمحوّل أول مرة | انتظر اكتمال التحميل (راقب اللوج)؛ الطلبات اللاحقة أسرع بكثير |

### فحص صحة المُرمِّز (tokenizer)

أول شي تفحصه لو الردود فاضية أو جملة ثابتة مكررة — الفحص ياخذ ثانية ويحسم إذا العطل بالموديل أو بالكود:

```bash
python3 -c "
from transformers import AutoProcessor
p = AutoProcessor.from_pretrained('ameer4wisam/gemma-iraqi-finetune-v2')
print('vocab:', p.tokenizer.vocab_size, '(المطلوب 262144)')
ids = p.tokenizer.encode('شكد سعر لابتوب؟')
print(ids[:10], '→', repr(p.tokenizer.decode(ids)))
"
```

**سليم** — مفردات كاملة والنص العربي يرجع كما هو:
```
vocab: 262144 (المطلوب 262144)
[236977, 68501, 178761, ...] → 'شكد سعر لابتوب؟'
```

**معطوب** — المفردات فاضية وكل النص صار `<unk>`:
```
vocab: 5 (المطلوب 262144)
[3] → '<unk>'
```

بحالة العطل، ارفع `tokenizer.json` من الموديل الأساس لمستودعك (يتطلب توكن بصلاحية **Write**):

```bash
export HF_HUB_ENABLE_HF_TRANSFER=0
python3 -c "
from transformers import AutoTokenizer
from huggingface_hub import upload_file
import os
tok = os.environ['HF_TOKEN']
t = AutoTokenizer.from_pretrained('google/gemma-4-12B-it', token=tok)
assert t.vocab_size == 262144, t.vocab_size
t.save_pretrained('/tmp/tk')
upload_file(path_or_fileobj='/tmp/tk/tokenizer.json', path_in_repo='tokenizer.json',
            repo_id='ameer4wisam/gemma-iraqi-finetune-v2', token=tok)
print('✅ رُفع tokenizer.json')
"
```

> **مهم**: `tokenizer.json` ملف مفردات ثابت من الموديل الأساس — التدريب/الدمج (`merge_and_unload`) ما يمسّه أبداً، فرفعه ما يأثر على أوزانك المدرَّبة إطلاقاً. الشرط الوحيد: `vocab_size` لازم يطابق `text_config.vocab_size` بـ `config.json` مالتك (262144) — مفردات من أساس مختلف تعطي كلام مشوّش بدل فراغ. ولا ترفع `tokenizer_config.json` من الأساس — الموجود بمستودعك جاي من عملية الدمج.
