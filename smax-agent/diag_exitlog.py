# -*- coding: utf-8 -*-
"""diag: يختبر تسجيل سبب الخروج في agent.py من غير ما يلمس الوكيل الشغّال.

بيشتغل في مجلد مؤقت (HERE بتتغيّر) عشان ماياثرش على agent_run.json الحقيقي
ولا على agent.log بتاع الإنتاج.

  python diag_exitlog.py
"""
import importlib
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

tmp = Path(tempfile.mkdtemp(prefix='exitlog_'))
import agent as A                                   # noqa: E402

# نحوّل كل الكتابة للمجلد المؤقت
A.RUN_FILE = tmp / 'agent_run.json'
A.LOG_FILE = tmp / 'agent.log'

ok = True


def check(name, cond):
    global ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)


def read_run():
    try:
        return json.loads(A.RUN_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def logtxt():
    try:
        return A.LOG_FILE.read_text(encoding='utf-8')
    except Exception:
        return ''


print('=== 1) بداية تشغيلة: الملف بيتكتب بـclean=False ===')
A._run.update({'pid': 1234, 'started': '2026-09-08 20:00:00',
               'clean': False, 'phase': 'بيستنى مهمة', 'job': '', 'exit': ''})
A._save_run()
r = read_run()
check('الملف اتكتب', bool(r))
check('clean=False في البداية', r.get('clean') is False)

print('=== 2) تغيّر المرحلة بيتسجّل، والتكرار مابيكتبش ===')
A.set_phase('بينفّذ مهمة', 'job-77')
r = read_run()
check('المرحلة اتحدّثت', r.get('phase') == 'بينفّذ مهمة')
check('المهمة اتسجّلت', r.get('job') == 'job-77')
before = A.RUN_FILE.stat().st_mtime_ns
A.set_phase('بينفّذ مهمة', 'job-77')
check('نفس المرحلة مابتكتبش تاني', A.RUN_FILE.stat().st_mtime_ns == before)

print('=== 3) خروج عادي بيتسجّل مرة واحدة ===')
A._exit_logged = False
A.mark_exit('خروج عادي')
r = read_run()
check('clean=True بعد الخروج', r.get('clean') is True)
check('السبب اتسجّل', r.get('exit') == 'خروج عادي')
check('آخر مهمة موجودة في اللوج', 'job-77' in logtxt())
n1 = logtxt().count('الوكيل بيقفل')
A.mark_exit('سبب تاني')
check('مافيش تسجيل مكرر', logtxt().count('الوكيل بيقفل') == n1)
check('السبب الأول مااتغيّرش', read_run().get('exit') == 'خروج عادي')

print('=== 4) تشغيلة اتقتلت من برّه (clean=False) بتتكشف ===')
A.RUN_FILE.write_text(json.dumps(
    {'pid': 999, 'started': '2026-09-08 16:00:00', 'clean': False,
     'phase': 'بينفّذ مهمة', 'job': 'job-88', 'exit': ''},
    ensure_ascii=False), encoding='utf-8')
A.report_previous_run()
t = logtxt()
check('اتقال إنها ماكتبتش سطر خروج', 'ماكتبتش سطر خروج' in t)
check('رقم العملية ظهر', '999' in t)
check('آخر مهمة ظهرت', 'job-88' in t)

print('=== 5) تشغيلة قفلت صح بتتقري صح ===')
A.RUN_FILE.write_text(json.dumps(
    {'pid': 1000, 'started': '2026-09-08 17:00:00', 'clean': True,
     'phase': 'بيستنى مهمة', 'job': '', 'exit': 'إشارة إيقاف (15)'},
    ensure_ascii=False), encoding='utf-8')
A.report_previous_run()
check('اتقال إنها قفلت صح', 'قفلت صح' in logtxt())

print('=== 6) استثناء غير ممسوك بيتكتب بالـtraceback ===')
A._exit_logged = False
try:
    raise ValueError('عطل تجريبي')
except ValueError:
    A._excepthook(*sys.exc_info())
t = logtxt()
check('الاستثناء اتسجّل', 'استثناء غير ممسوك' in t)
check('الـtraceback موجود', 'ValueError' in t)
check('الخروج اتسجّل بالسبب', read_run().get('exit', '').startswith('استثناء'))

print('=== 7) الأسرار مابتتطبعش ===')
A._SECRETS.append('SUPER-SECRET-VALUE-123')
A._exit_logged = False
A.mark_exit('خروج فيه SUPER-SECRET-VALUE-123 جوّه')
check('السر اتشال من اللوج', 'SUPER-SECRET-VALUE-123' not in logtxt())

print()
print('=' * 52)
print('✔ كل اختبارات تسجيل الخروج نجحت' if ok else '✘ فيه اختبار فشل')
sys.exit(0 if ok else 1)
