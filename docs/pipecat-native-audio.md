# مراجعة Pipecat والصوت الأصلي والذاكرة — 2026-10-17

التنفيذ في `E:\AMEER\all_project\app\back_end_iraqi`، واختبار التكامل في `E:\rust_project\call_center`.
المكالمات تستعمل Pipecat فقط؛ أزيل العامل وملف محوّل LiveKit وتبعياته من بيئة الخادم.
أزيل Whisper بالكامل: ملفه، إعداداته، تحميله المسبق، ومسار النسخ المحلي للرسائل الصوتية.
بقيت transformers لأنها تبعية للنطق وvLLM؛ لا يوجد استدعاء `pipeline("automatic-speech-recognition")`.

## المسار الحالي

واتساب ← Rust ← PCM s16le mono 16kHz عبر WebSocket ← VAD على CPU ← WAV داخل `input_audio` إلى Gemma.
النموذج يرجع نسخ كلام العميل داخل وسم داخلي، ثم الرد والأدوات؛ يُحذف الوسم قبل النطق.
Pipecat يشغّل قراءة الرد وتجميع العبارات وتوليد الصوت وإرساله بالتوازي، مع إلغاء الجميع عند المقاطعة.
Chatterbox باقٍ للنطق: [Gemma يفهم الصوت ويولّد نصاً](https://huggingface.co/google/gemma-4-E4B-it)، ولا ينتج PCM للمكالمة بنفسه.

الافتتاحية تُجهّز أثناء الرنين. لا PCM ولا نص وكيل يصل إلى التطبيق قبل `accepted`.
الجملة الأولى تبدأ عند فاصل قصير أو نحو 56 حرفاً، بدل انتظار جملة طويلة.
سكوت نهاية دور العميل صار 400ms بدلاً من 700ms. هذا يخفض الانتظار المحدد بالإعداد 300ms؛ ليس قياساً لأداء النموذج.
مخزن التطبيق الصوتي بقي 120ms لامتصاص تفاوت الشبكة، والإطارات بقيت 20ms→60ms كما كانت.

## الأعطال والدليل والإصلاح

| العطل | الدليل | الملف/الموضع | الإصلاح | الاختبار |
|---|---|---|---|---|
| الصوت ممنوع في vLLM رغم دعم E4B له | مثبت: `audio:0` سابقاً، وبطاقة النموذج وتوثيق vLLM | `app/core/vllm_launch.py:6` | تفعيل إدخال صوت واحد وصورة واحدة | `test_vllm_memory_plan.py` و`test_native_llm_stream.py` |
| Whisper محمّل مرتين داخل FastAPI | مثبت: محمّلان مستقلان في transcribe.py وstt.py | `app/features/order_intake/transcribe.py`؛ حذف `voice_followup/stt.py` | فهم الصوت بواسطة النموذج الحالي؛ تقسيم الرسائل المسجلة إلى 28 ثانية مع تراكب | `test_speech_model_config.py` |
| انتظار الجملة والنطق والشبكة بالتسلسل | مثبت من حلقة `_respond/_speak` القديمة | `voice_followup/pipecat_voice.py` و`app/features/voice_followup/stream.py:172` | مراحل Pipecat متوازية، عبارة أولى قصيرة وتجهيز أثناء الرنين | `test_first_phrase_starts_before_a_long_sentence_finishes` واختبار blocked playback وprewarm |
| المقاطعة تنتظر اكتمال النموذج عند دمج Pipecat | مثبت: اختبار الإلغاء فشل، وانتظر 60 ثانية قبل الإصلاح | `app/features/voice_followup/pipecat_voice.py:122` | إرسال EndFrame بعد اكتمال التشغيل فقط؛ CancelFrame يبقى قادراً على الإلغاء | `test_pipecat_cancellation_closes_llm_and_its_tasks` |
| سجل المساعد يُضاف بعد دور العميل عند المقاطعة | مثبت: اختباران فشلا قبل ترتيب الإلغاء | `app/features/voice_followup/stream.py:493` | إنهاء تسجيل الرد السابق قبل إدخال دور العميل | اختبار الدور الكامل والمقاطعة في `test_voice_followup_stream.py` |
| موت عامل صوت يترك المكالمة تنتظر | مثبت: انتظار `_stop` دون مراقبة استثناءات المهام | `app/features/voice_followup/stream.py:337` | إظهار الخطأ وإنهاء الجلسة؛ طابور محدود ووقت إرسال محدد | اختبارات worker failure وbackpressure وsend timeout |
| الرد المقطوع أو SSE التالف يمر بلا خطأ | مثبت من تجاهل JSON/finish_reason في القارئ القديم | `app/features/voice_followup/stream.py:82` | رفض التقطيع والتدفق الناقص وJSON التالف؛ مساحة إضافية لنسخ الكلام | `test_native_llm_stream.py` |
| اتصال واتساب يبدأ والخدمة لا تستطيع الكلام | مثبت: ready قبل التحقق من التبعيات | `app/features/voice_followup/stream.py:737` | رفض مبكر واضح عند غياب النموذج أو النطق أو أثناء الإحماء | `test_real_dependencies_are_checked_before_rust_is_told_to_dial` |
| Rust يحسب نهاية العطل الصوتي نجاحاً للحملة | مثبت: كان يتجاهل reason في end_call | `call_center/crates/app/src/ai_call.rs:agent_end_reason` | أسباب الأخطاء تُحفظ ولا تُحسب نجاحاً | `service_audio_failure_does_not_complete_a_voice_campaign_successfully` |
| لوحة الخادم الوهمية تقبل عقداً يرفضه الخادم الحقيقي | مثبت: كانت ترسل call_type/order بدل company_name/goal | `static/index.html` | مثال واحد مطابق للعقد وإجابة Pipecat | `test_console_example_is_accepted_by_the_real_api` و`console-contract.test.mjs` |
| سجلات INFO لا تظهر مع إعداد uvicorn الافتراضي | مثبت: لا إعداد logging للتطبيق | `app/core/voice_logging.py` | تفعيل سجلات التشغيل المحددة دون إغراق Pipecat DEBUG | اختبار ظهور سطر الملخص وعدم تكراره |
| فشل التثبيت على Ubuntu 22.04 الخام | مثبت: Python 3.10 مقابل متطلب Pipecat >=3.11 | `start.sh` | بيئة Python 3.12.12 معزولة عند الحاجة، دون تغيير Python النظام | فحص Bash؛ تشغيل Linux الفعلي يتطلب خادم GPU |

## لماذا VRAM تبدو ممتلئة؟

هذا ليس دليلاً كافياً على تسرب. [توثيق vLLM](https://docs.vllm.ai/en/latest/configuration/conserving_memory/)
يفصل الأوزان والتفعيلات وCUDA graphs وكاش KV. افتراضياً كان التطبيق يتيح لـvLLM حجز 85%، ثم يحمّل
نسختين من Whisper ويسمح بنسختين من Chatterbox على الكرت نفسه. لذلك حجم الملفات وحده لا يفسّر الاستهلاك.

بطاقة Gemma E4B تذكر نحو 8B معلمة كلية، مع نحو 4.5B فعالة؛ الاسم E4B لا يعني أوزان 4GB.
نحو 8B × 2 بايت ≈16GB للأوزان BF16 حسابياً، قبل الكاش والتفعيلات والنطق.

الإعدادات الجديدة:

| الإعداد | الافتراضي | معناه |
|---|---:|---|
| `KV_CACHE_MEMORY_GB` | 4 | سقف KV فقط، لا إجمالي VRAM |
| `GPU_MEMORY_UTILIZATION` | 0.70 | يُستخدم لتحجيم الكاش فقط عند `KV_CACHE_MEMORY_GB=0` |
| `VLLM_MAX_NUM_SEQS` | 16 | بدلاً من 90 لتقليل التوازي والذاكرة المرتبطة به |
| `TTS_POOL_SIZE` | 1 | نسخة نطق واحدة بدلاً من نسختين؛ زيادتها تحسّن توازي النطق وتزيد الذاكرة |

هذه القيم قابلة للتعديل، ويطبع الإقلاع `voice_memory_plan`. لا نفترض أنها تكفي لكل كرت أو تكميم أو طول سياق.
سقف KV الصريح يتجاوز التحجيم بالنسبة وفق [توثيق CacheConfig](https://docs.vllm.ai/en/latest/api/vllm/config/cache/).
خفض طول السياق وحده لا يعني أن كاش vLLM سيترك كل المساحة الحرة؛ لذلك أضيف سقف KV مستقل.

`GET /health/voice` يعرض ذاكرة الكرت كلها وذاكرة Python المخصصة والمحجوزة والكاش غير المستخدم كلّاً على حدة.
ما يزال «تسرب مستمر بعد مكالمات متكررة» **مشتبهاً غير مثبت**: قارن القياسات بعد الإحماء ثم بعد 20 مكالمة بنفس الحمل.
لا يُستعمل `empty_cache()` بعد كل عبارة؛ فهو لا يحرر الأوزان أو KV الحي وقد يزيد التأخير.

## التحقق الذي أُنجز وحدوده

نجح عميل Rust الحقيقي ضد FastAPI وPipecat الحقيقيين عبر socket محلي: start/ready، قبول، PCM في الاتجاهين،
نص العميل والوكيل، أدوات حفظ النتيجة والإنهاء. النموذج والنطق في هذا الاختبار محاكيان؛ لا اتصال بزبائن أو خدمات خارجية.
جُرّب Silero ONNX المرفق مع Pipecat على CPU. أزيلت حزم LiveKit من بيئة الخادم، و`pip check` نجح.

الجهاز المحلي يملك RTX 5060 Laptop 8GB، وبيئة Python الحالية Torch CPU؛ لا يطابق خادم vLLM/Linux/GPU.
لم تُنزّل أوزان Gemma/Chatterbox الكبيرة، ولم يُدّعَ قياس زمنهما الحقيقي أو جودة فهم اللهجة.
نُظّفت حزمة الاختبار والكاش المؤقت داخل مشروع call_center بعد الفحوص؛ تبعيات التشغيل المثبتة في بيئة الخادم بقيت.

حد Gemma للصوت في الدور 30 ثانية حسب بطاقة النموذج. تجاوز ذلك في المكالمة يظهر خطأ وينهيها، ولا يُقص الصوت بصمت.
رفع الرسائل المسجلة يقبل حتى 25MB/10 دقائق ويقسمها. فشل النطق أو قالب النسخ الداخلي يظهر خطأ صريحاً.

## ما يُجرّب على واتساب الحقيقي

1. أعد تشغيل الخادم بـ`bash start.sh`، ثم افحص `/health/voice`: النموذج جاهز، والنطق محمّل، و`engine=pipecat`.
2. اتصل برقم تجريبي، واتركه يرن قليلاً ثم أجب: ابحث عن `voice_first_audio ... accepted_to_audio_ms=... prewarmed=True`.
3. احكِ بلهجة عراقية واقطع كلام الوكيل: ابحث عن `voice_turn_end ... status=interrupted` و`interrupts` في `voice_call_end`.
4. أكمل هدف المكالمة: راقب النتيجة في التطبيق وسطر `voice_call_end ... reason=agent response_errors=0`، ثم نتيجة الحملة.
5. اختبر قطع الخادم أو تعطيل النطق: يجب ألا تُسجل الحملة نجاحاً. سببها يبقى ظاهراً في التطبيق والسجل.
6. أرسل رسالة صوتية أطول من 30 ثانية إلى مسار الطلبات: ابحث عن `audio_transcription_end ... chunks=2` أو أكثر.
7. راقب `nvidia-smi` و`/health/voice` بعد الإحماء وبعد 20 مكالمة. راقب وزن الذاكرة المخصصة لكل عملية، لا نسبة الكرت وحدها.

السجلات الجديدة لا تحتوي نص المكالمة أو الاسم أو الهاتف؛ `trace` عشوائي للربط بين أسطر العملية.
سجل Python يبقى في خرج الخادم؛ سجل Rust في `%LOCALAPPDATA%\org.ruyahcenter.callcenter\logs\call-center.log`.
الفحوص التفصيلية النهائية موجودة في خاتمة تقرير التسليم؛ لا commit ولا نشر على خادم بعيد.

## نتائج الفحص النهائية

- Python: 1124 ناجحاً، 100 متخطّى لعدم وجود خادم النموذج باختبارات المواقع الحية.
- Rust: 253 اختباراً ناجحاً و11 متجاهلاً افتراضياً؛ وفحوص Clippy مع -D warnings وfmt ناجحة.
- اختبار Rust مع خادم Pipecat الحقيقي: ناجح، مع نموذج ونطق محاكيين.
- TypeScript: ناجح؛ اختبارات الواجهة 11/11، ولوحة الخادم 2/2.
- pip check وفحص Bash: ناجحان. تشغيل vLLM وGemma/Chatterbox على GPU الحقيقي لم يُنفّذ محلياً.
