# مساحة تدريب نموذج تصحيح المناطق

مجلد مستقل عن الخدمة: يجهّز بيانات مرجعية (gold labels) بمساعدة ذكاء اصطناعي، ويقيس دقة الخدمة عليها، ويدرّب نموذج Reranker صغيرًا يختار المنطقة من بين مرشحي الكتالوج. لا تتغير الخدمة بشيء مما هنا حتى يثبت النموذج تفوّقه في القياس.

## البيئة

بيئة منفصلة داخل المجلد. الموديلات المنزّلة تُحفظ في `training/.venv/hf-cache`.

```powershell
py -3.14 -m venv training\.venv
training\.venv\Scripts\python -m pip install -r training\requirements.txt
# لتدريب على كرت الشاشة (RTX 50xx):
training\.venv\Scripts\python -m pip install torch --index-url https://download.pytorch.org/whl/cu128
```

## المسار

1. **تصدير الدفعات:** كل ملف يخص شركة ومحافظة، ومعه قائمة مناطقها ونتيجة النظام الحالي.
   ```powershell
   training\.venv\Scripts\python training\export_batches.py "JSON_to_Excel (1).xlsx" --sheets FUHOOD_3,FUHOOD_4 --skip-labeled
   ```
   لقياس الخدمة كاملة (القواعد + LLM) بدل القواعد وحدها، أضف `--api-url http://127.0.0.1:8000 --api-key <district_api_key>`.
2. **الوسم بالذكاء الاصطناعي:** انسخ [prompts/labeling_prompt.md](prompts/labeling_prompt.md) ثم محتوى ملف دفعة واحد إلى نموذج قوي (Claude أو GPT)، واحفظ رده (JSONL) في `training/data/labels/<اسم>.jsonl`.
3. **الفحص والدمج:**
   ```powershell
   training\.venv\Scripts\python training\validate_labels.py
   ```
   يرفض أي اسم ليس في الكتالوج، ويجمع النتيجة في `data/gold.jsonl`. الصفوف التي تردّد فيها الوسم (`confidence: low`) تذهب إلى `data/review.jsonl`: صحّحها واحفظها باسم فيه `reviewed` مع `"confidence": "high"`، مثل `data/labels/FUHOOD_2.reviewed.jsonl`، فتتقدّم على وسم الموديل.
4. **القياس:**
   ```powershell
   training\.venv\Scripts\python training\evaluate.py
   ```
   يعطي الدقة، وتوزيع الأخطاء (`WRONG_DISTRICT`، `TOO_GENERAL`، `MISSED`، `FALSE_MATCH`)، وقائمة الصفوف الخاطئة في `data/errors.jsonl`.
5. **التدريب ثم المقارنة:**
   ```powershell
   training\.venv\Scripts\python training\train_reranker.py --epochs 3
   training\.venv\Scripts\python training\evaluate.py --model training\models\reranker
   ```
   يُطبع في نهاية التدريب دقة القواعد ودقة النموذج على صفوف لم يرها النموذج أثناء التدريب. النصوص المتكررة لا تُقسَم بين التدريب والاختبار.

## متى يكون النموذج جاهزًا

- لا يُعتمد قبل **بضعة آلاف** من الصفوف المرجعية من أكثر من شركة.
- يُعتمد فقط إذا تفوّق على القواعد في الصفوف المحجوزة للاختبار، **دون** زيادة في `FALSE_MATCH` و`WRONG_DISTRICT`، لأن هذين الخطأين يرسلان الشحنة لمكان خاطئ.
- عند ربطه بالخدمة تبقى دروع التحقق كما هي حوله: الاسم من الكتالوج فقط، ومكتوب في النص، وغير مخالف له.

## الحالة الحالية

- عيّنة مرجعية واحدة: ورقة FUHOOD_2، 163 صفًا وسمها Claude، منها 5 صفوف تنتظر المراجعة البشرية.
- القواعد وحدها: **94.9%** على 158 صفًا. هذه الورقة نفسها بُنيت عليها إصلاحات القواعد، فالرقم أعلى من المتوقع على بيانات جديدة.
- تجربة التدريب على 122 صفًا: **94.3%**. المسار يعمل، والبيانات لا تكفي بعد.
