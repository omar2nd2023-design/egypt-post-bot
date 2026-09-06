# -*- coding: utf-8 -*-
"""smax_search.py — طبقة البحث الحي في SMAX.

مبنية على ميكانيكا اتثبتت بالمشاهدة:
  • إضافة فلتر قياسي  : a[title="Add filter"] -> select2
  • إضافة فلتر عمود   : .slick-header-column (SlickGrid) — للأعمدة المخصّصة
                        زي Shipment No و Request number اللي **مش** في قائمة
                        الفلاتر القياسية
  • مؤشر النجاح       : "N total items" تحت الشبكة
  • الفلاتر AND       : لازم نشيل اللي قبله قبل ما نجرّب اللي بعده

⚠️ مابيطبعش: بيانات دخول · كوكيز · محتوى الجلسة.
"""
import re
import time

GRID_URL = "https://support.degypt.net/saw/Requests"
GROUP_VALUE = "Egypt Post"
SETTLE = 1500   # كان 2500 — تحسين 2026-09-06 (القوائم بتظهر خلال ~1 ث)


# ------------------------------------------------------------ قراءة الحالة
def read_count(page):
    """عدد النتائج من العدّاد. None = الشبكة مش معروضة."""
    try:
        m = re.search(r"(\d[\d,]*)\s*total items",
                      page.evaluate("() => document.body.innerText"))
        return int(m.group(1).replace(",", "")) if m else None
    except Exception:
        return None


def too_many(page):
    try:
        return "too many records" in page.evaluate(
            "() => document.body.innerText").lower()
    except Exception:
        return False


def filter_labels(page):
    """أسماء الفلاتر الظاهرة في الشريط."""
    try:
        return page.evaluate("""() => {
          const out=[];
          document.querySelectorAll('.platform-filter-panel *, [class*=filter-item]').forEach(e=>{
            const t=(e.innerText||'').trim();
            if (t && t.length<60 && /:/.test(t) && e.children.length<=3) out.push(t.replace(/\\s+/g,' '));
          });
          return [...new Set(out)];
        }""")
    except Exception:
        return []


# ------------------------------------------------------------ تعديل الفلاتر
def clear_all(page):
    """يشيل كل الفلاتر. الشبكة هتقول 'too many records' لحد ما نضيف فلتر ضيّق."""
    try:
        ca = page.get_by_text("Clear all", exact=False)
        if ca.count():
            ca.first.click()
            page.wait_for_timeout(4000)
            return True
    except Exception:
        pass
    return False


def _pick(page, text, exact=True):
    """يضغط على خيار select2 بنصّه — مش Enter (Enter بياخد أول خيار)."""
    page.wait_for_timeout(SETTLE)
    if exact:
        o = page.locator(".select2-result-label").filter(
            has_text=re.compile(r"^%s$" % re.escape(text)))
        if o.count():
            o.first.click(); page.wait_for_timeout(4000); return True
    o = page.locator(".select2-result-label", has_text=text)
    if o.count():
        o.first.click(); page.wait_for_timeout(4000); return True
    return False


def add_active_filter(page):
    """Active = Yes — قائمة بسيطة (No/Yes) مش select2."""
    try:
        page.locator('a[title="Add filter"]').first.click()
        page.wait_for_timeout(SETTLE)
        page.locator("input.select2-input.select2-focused").first.fill("Active")
        if not _pick(page, "Active"):
            return False
        page.wait_for_timeout(2000)
        y = page.locator("li, a, span, div").filter(
            has_text=re.compile(r"^Yes$")).filter(visible=True)
        if y.count():
            y.first.click()
            page.wait_for_timeout(5000)
            return True
    except Exception:
        pass
    return False


def setup_baseline(page, log=print):
    """يجهّز الحالة قبل البحث — **من غير Clear all**.

    🔴 Clear all بيشيل الأساس كمان، والشبكة بتقول 'too many records'
       وبتختفي الأعمدة — فمش بنقدر نضيف فلتر عمود بعدها. اتجرّب وفشل.
    الصح: نشيل فلاتر **القيم** بس، ونسيب Active والمجموعة.
    """
    KEEP = ("Active", "Current assignment group")
    # 🔴 المجموعة **الأول** وبعدين نشيل فلاتر القيم — لو شلنا القيم وإحنا من
    #    غير مجموعة (زي ما بيحصل بعد بحث شامل سابق) الشبكة بتقع على
    #    'too many records' والعدّاد بيبقى None → baseline_failed. حصلت فعلاً.
    # مسار سريع (2026-09-06): الأساس سليم أصلاً — Active=Yes + المجموعة بس
    # والعدّاد ظاهر → مافيش أي تعديل ولا انتظار (بيوفّر ~30–40 ث).
    names = [n.lower() for n in chip_names(page)]
    if (sorted(names) == sorted(k.lower() for k in KEEP)
            and (chip_value(page, "Active") or "").lower() == "yes"
            and (chip_value(page, "Current assignment group") or "").lower()
                == GROUP_VALUE.lower()):
        c = read_count(page)
        if c is not None:
            log("   الأساس سليم — العدّاد=%s" % c)
            return True
    if not chip_exists(page, "Current assignment group"):
        add_group_filter(page)
    set_active(page, "Yes")          # لو بحث سابق سابها No
    for n in chip_names(page):
        if n and not any(n.lower().startswith(k.lower()) for k in KEEP):
            remove_chip(page, n)
    sanitize_group_chip(page)
    c = read_count(page)
    # طبقة دفاع تانية: لو العدّاد لسه مش ظاهر (الشبكة بتحمّل) نستنّاه لحد 15 ث
    # قبل ما نحكم "baseline_failed".
    waited = 0
    while c is None and waited < 15000:
        page.wait_for_timeout(1000)
        waited += 1000
        c = read_count(page)
    log("   الأساس جاهز — العدّاد=%s" % c)
    return c is not None


def add_group_filter(page, value=GROUP_VALUE):
    """Current assignment group = Egypt Post — عن طريق قائمة الفلاتر."""
    page.locator('a[title="Add filter"]').first.click()
    page.wait_for_timeout(SETTLE)
    page.locator("input.select2-input.select2-focused").first.fill(
        "Current assignment group")
    if not _pick(page, "Current assignment group"):
        return False
    vis = page.locator("input.select2-input:visible")
    if not vis.count():
        return False
    vis.last.fill(value)
    ok = _pick(page, value)
    # 🔴 الاختيار لوحده **مابيطبّقش** — المحرّر بيفضل مفتوح والشبكة تفضل على
    #    'too many records'، ولو الصفحة اتقفلت الفلتر بيضيع. لازم تأكيد
    #    (Enter + ضغطة برّه) زي فلاتر الأعمدة بالظبط. اتشاف بالصورة.
    _commit(page)
    return ok


def remove_group_filter(page):
    """يشيل chip المجموعة بس — الفلاتر التانية تفضل."""
    try:
        n = page.evaluate("""() => {
          let hit = 0;
          document.querySelectorAll('*').forEach(e => {
            const t=(e.innerText||'').trim();
            if (hit) return;
            if (/^Current assignment group\\s*:/.test(t) && e.children.length <= 6) {
              const x = e.querySelector('.icon-remove,.icon-close,[class*=remove],[class*=close]');
              if (x) { x.click(); hit = 1; }
            }
          });
          return hit;
        }""")
        page.wait_for_timeout(5000)
        return bool(n)
    except Exception:
        return False


def _scroll_header_into_view(page, column, tries=8):
    """يرجّع إحداثيات رأس العمود بعد ما يجيبه جوه الشاشة، أو None."""
    for _ in range(tries):
        box = page.evaluate("""(name) => {
          const h=[...document.querySelectorAll('.slick-header-column')].find(e=>{
            const s=e.querySelector('.slick-column-name');
            return s && s.innerText.trim()===name;});
          if(!h) return null;
          const r=h.getBoundingClientRect();
          return {x:r.left+r.width/2, y:r.top+r.height/2,
                  left:r.left, right:r.right, w:r.width, off:h.offsetLeft};
        }""", column)
        if not box:
            return None
        if box["w"] > 5 and 40 < box["left"] and box["right"] < 1580:
            return box
        # 🔴 مع 0 صف الـviewport فاضي ومابيتزحلقش، فالرؤوس بتفضل مزحلقة برّه
        #    الشاشة (left سالب). بنزحلق **كل حاوية رؤوس قابلة للتمرير** مباشرة.
        # 🔴 offsetLeft محسوب نسبةً لحاوية عندها left:-1000px فمابينفعش كهدف
        #    مطلق — بنزحلق **بالفرق** من مكان الرأس الحالي على الشاشة.
        page.evaluate("""(left) => {
          const d = Math.round(left - 400);
          const vp=document.querySelector('.slick-viewport');
          if(vp) vp.scrollLeft = Math.max(0, vp.scrollLeft + d);
          const h=document.querySelector('.slick-header-column');
          for (let e = h && h.parentElement; e; e = e.parentElement) {
            if (e.scrollWidth > e.clientWidth + 5 || e.scrollLeft > 0)
              e.scrollLeft = Math.max(0, e.scrollLeft + d);
          }
        }""", box["left"])
        page.wait_for_timeout(1000)
    return None


def filter_by_column(page, column, value, tries=4):
    """فلتر عمود مخصّص (Shipment No / Request number).

    🔴 الضغط على رأس العمود نفسه **مابيعملش حاجة**. كل رأس فيه زرار
       مخفي `div.slick-header-button.slick-header-button-hidden` —
       بيظهر لما تحوّم بالماوس على الرأس، والضغط عليه هو اللي بيضيف
       الفلتر. جرّبت الضغط على الرأس (JS وماوس حقيقي) وفشل الاتنين.
    """
    before = read_count(page)

    if not chip_exists(page, column):
        box = _scroll_header_into_view(page, column)
        if not box:
            return None, "column_not_reachable"
        # التحويم بيشيل الـhidden عن زرار الفلتر
        page.mouse.move(box["x"], box["y"])
        page.wait_for_timeout(1200)
        btn = page.evaluate("""(name) => {
          const h=[...document.querySelectorAll('.slick-header-column')].find(e=>{
            const s=e.querySelector('.slick-column-name');
            return s && s.innerText.trim()===name;});
          if(!h) return null;
          const b=h.querySelector('.slick-header-button');
          if(!b) return null;
          const r=b.getBoundingClientRect();
          if(!r.width) return {hidden:true};
          return {x:r.left+r.width/2, y:r.top+r.height/2};
        }""", column)
        if not btn or btn.get("hidden"):
            return None, "filter_button_hidden"
        page.mouse.click(btn["x"], btn["y"])
        # الـchip بيظهر خلال ~1 ث — بنستنى ظهوره بدل 4 ث ثابتة (حد 4 ث)
        for _ in range(8):
            page.wait_for_timeout(500)
            if chip_exists(page, column):
                break
        if not chip_exists(page, column):
            return None, "chip_not_created"
    else:
        if chip_value(page, column) == str(value):
            # نفس القيمة موجودة أصلاً — مافيش داعي نعيد كتابتها (بتوفّر ~20 ث)
            return read_count(page), "ok"
        if not open_chip_editor(page, column):
            return None, "editor_not_opened"

    for _ in range(tries):
        box = page.locator("pl-filter-field input[type=text]:visible")
        if box.count() == 0:
            box = page.locator(".platform-filter-panel input[type=text]:visible")
        if box.count() == 0:
            return read_count(page), "input_not_found"
        try:
            box.last.fill(str(value))
        except Exception:
            pass
        _commit(page, before=before)
        now = read_count(page)
        if now is not None and now != before:
            return now, "ok"
        if now == 0:
            return 0, "ok"
        if not open_chip_editor(page, column):
            break
    return read_count(page), "uncertain"

def _apply_value(page, value, before, tries=6):
    """يكتب القيمة ويضغط Enter لحد ما العدّاد يتغيّر.

    🔴 لازم **نعيد تحديد** حقل الإدخال قبل كل ضغطة: الصفحة بتعيد بناء
       الـchip بعد أول Enter، فالمرجع القديم بيبوظ وPlaywright بيرمي خطأ.
    🔴 وEnter مش مضمون من أول مرة (تأكيد المستخدم) — بنكرر لحد ما يبان
       **دليل** (العدّاد اتغيّر)، مش لعدد ثابت ولا انتظار زمني.
    """
    def box():
        b = page.locator(".platform-filter-panel input[type=text]:visible")
        if b.count() == 0:
            b = page.locator("input[type=text]:visible")
        return b.last if b.count() else None

    for i in range(tries):
        try:
            bx = box()
            # حقل الإدخال بيظهر بعد اختيار الحقل بجزء من ثانية — بنستنى ظهوره
            # (حد 4 ث) بدل ما نحكم "input_gone" فورًا (حصل بعد تقليل SETTLE).
            for _ in range(8):
                if bx is not None:
                    break
                page.wait_for_timeout(500)
                bx = box()
            if bx is None:
                # بعد أول Enter المحرّر بيتقفل والحقل بيختفي = القيمة اتطبّقت
                # (العدّاد ممكن مايتغيّرش لو النتيجة 0 → 0). مش خطأ.
                return read_count(page), ("ok" if i > 0 else "input_gone")
            if i == 0 or (bx.input_value() or "") != str(value):
                bx.fill(str(value))
                page.wait_for_timeout(600)
            bx.press("Enter")
        except Exception:
            page.keyboard.press("Enter")
        # انتظار على دليل (2026-09-06): العدّاد يتغيّر — بحد 3.5 ث بدل 3.5 ث ثابتة
        now = None
        for _ in range(7):
            page.wait_for_timeout(500)
            now = read_count(page)
            if now is not None and now != before:
                page.wait_for_timeout(600)
                return now, "ok"
    now = read_count(page)
    return now, ("ok" if now is not None else "enter_uncertain")


# =========================================================================
# ميكانيكا الفلاتر — مثبتة بالتجربة على الواجهة الحقيقية
# =========================================================================
# الـchip عنصر <pl-filter-field> فيه جزئين:
#   .platform-filter-field-predicate-viewer   عرض القيم (ضغطة عليه تفتح المحرّر)
#   .platform-filter-field-predicate-editor   المحرّر (ng-hide لحد ما يتفتح)
# والتثبيت بيتم بـEnter + ضغطة بره الـchip.
# ⚠️ من غير فتح المحرّر، أي تعديل على القيم **مابيتحفظش**.

def _commit(page, wait=6000, before=None):
    """تثبيت تعديل الفلتر: Enter ثم ضغطة بره.

    تحسين 2026-09-06: انتظار على **دليل** — لو عندنا العدّاد اللي قبل التعديل
    بنستنى لحد ما يتغيّر (بحد أقصى `wait`) بدل انتظار ثابت. لو ماتغيّرش
    (نتيجة 0 → 0 مثلًا) بنستنى الحد كله زي الأول — السلوك ماينقصش.
    """
    try:
        page.keyboard.press("Enter")
        page.wait_for_timeout(800)
        page.mouse.click(1200, 700)
    except Exception:
        pass
    if before is None:
        page.wait_for_timeout(min(wait, 4000))
        return
    waited = 0
    while waited < wait:
        page.wait_for_timeout(400)
        waited += 400
        now = read_count(page)
        if now is not None and now != before:
            page.wait_for_timeout(700)      # الشبكة بترسم الصفوف بعد العدّاد
            return


def chip_exists(page, name):
    return bool(page.evaluate("""(n) => !![...document.querySelectorAll('pl-filter-field')]
        .find(e => new RegExp('^' + n, 'i').test((e.innerText||'').trim()))""", name))


def chip_value(page, name):
    """قيمة الـchip كنص (اللي بعد النقطتين) — أو None لو مش موجود."""
    try:
        t = page.evaluate("""(n) => {
          const c=[...document.querySelectorAll('pl-filter-field')]
            .find(e => new RegExp('^' + n, 'i').test((e.innerText||'').trim()));
          return c ? (c.innerText||'').replace(/\\s+/g,' ').trim() : null;
        }""", name)
        if t is None or ":" not in t:
            return None
        return t.split(":", 1)[1].strip()
    except Exception:
        return None


def chip_names(page):
    """أسماء كل الـchips الظاهرة (قبل النقطتين)."""
    return page.evaluate("""() => [...document.querySelectorAll('pl-filter-field')]
        .map(e => ((e.innerText||'').trim().split(':')[0]||'').trim()).filter(Boolean)""") or []


def open_chip_editor(page, name):
    r = page.evaluate("""(n) => {
      const c=[...document.querySelectorAll('pl-filter-field')]
        .find(e => new RegExp('^' + n, 'i').test((e.innerText||'').trim()));
      if(!c) return 'no_chip';
      const v=c.querySelector('.platform-filter-field-predicate-viewer');
      if(!v) return 'no_viewer';
      v.click(); return 'ok';
    }""", name)
    # المحرّر بيظهر خلال أقل من ثانية — بنستنى ظهوره (حد 3 ث) بدل 3 ث ثابتة
    for _ in range(6):
        page.wait_for_timeout(500)
        try:
            if page.locator(".platform-filter-field-predicate-editor:visible").count():
                break
        except Exception:
            pass
    return r == "ok"


def set_active(page, value):
    """Active = Yes / No — قائمة بسيطة جوه محرّر الـchip (مثبتة بالتجربة
    2026-09-06: `applyBooleanEditor`). بترجّع True لو الـchip بقى بالقيمة."""
    value = "Yes" if str(value).lower().startswith("y") else "No"
    if (chip_value(page, "Active") or "").lower() == value.lower():
        return True
    if not chip_exists(page, "Active"):
        return add_active_filter(page) if value == "Yes" else False
    before = read_count(page)
    if not open_chip_editor(page, "Active"):
        return False
    opt = page.locator(".platform-filter-field-predicate-editor:visible").locator(
        "text=/^%s$/" % value)
    if not opt.count():
        return False
    opt.first.click()
    page.wait_for_timeout(800)
    _commit(page, before=before)
    return (chip_value(page, "Active") or "").lower() == value.lower()


def chip_tokens(page, name):
    return page.evaluate("""(n) => {
      const c=[...document.querySelectorAll('pl-filter-field')]
        .find(e => new RegExp('^' + n, 'i').test((e.innerText||'').trim()));
      if(!c) return [];
      const ed=c.querySelector('.platform-filter-field-predicate-editor');
      const src = ed && !ed.className.includes('ng-hide') ? ed : c;
      return [...src.querySelectorAll('.select2-search-choice')]
               .map(t=>(t.innerText||'').trim());
    }""", name) or []


def remove_chip_token(page, name, token):
    r = page.evaluate("""(a) => {
      const [n, tok] = a;
      const c=[...document.querySelectorAll('pl-filter-field')]
        .find(e => new RegExp('^' + n, 'i').test((e.innerText||'').trim()));
      if(!c) return 'no_chip';
      for (const tk of c.querySelectorAll('.select2-search-choice')) {
        if ((tk.innerText||'').trim().toLowerCase() === tok.toLowerCase()) {
          const x=tk.querySelector('.select2-search-choice-close');
          if (x) { x.click(); return 'ok'; }
        }
      }
      return 'token_not_found';
    }""", [name, token])
    page.wait_for_timeout(2000)
    return r == "ok"


def remove_chip(page, name):
    """يشيل الفلتر كله."""
    r = page.evaluate("""(n) => {
      const c=[...document.querySelectorAll('pl-filter-field')]
        .find(e => new RegExp('^' + n, 'i').test((e.innerText||'').trim()));
      if(!c) return 'no_chip';
      const x=c.querySelector('.platform-filter-field-remove, .icon-delete');
      if(!x) return 'no_remove';
      x.click(); return 'ok';
    }""", name)
    # الـchip بيختفي فورًا؛ الشبكة بتتحدّث بعد الـOK اللي بييجي بعدها (reapply)
    for _ in range(6):
        page.wait_for_timeout(400)
        if not chip_exists(page, name):
            break
    page.wait_for_timeout(600)
    return r == "ok"


def sanitize_group_chip(page):
    """يتأكد إن chip المجموعة قيمته Egypt Post بس.

    🔴 قيمة (no value) بتتسلل لو حد ضغط Enter على قائمة القيم — ووقتها
       النتيجة بتبقى ضخمة والشبكة بتقول 'too many records'. حصل فعلاً.
    """
    if not chip_exists(page, "Current assignment group"):
        return False
    toks = chip_tokens(page, "Current assignment group")
    bad = [x for x in toks if x.lower() != GROUP_VALUE.lower()]
    if not bad:
        return True
    if not open_chip_editor(page, "Current assignment group"):
        return False
    for tok in bad:
        remove_chip_token(page, "Current assignment group", tok)
    _commit(page)
    return read_count(page) is not None

# ------------------------------------------------------------ الجلسة
def _wait_grid_or_login(page, max_ms=20000):
    """انتظار على دليل بعد التحميل: إمّا شريط الفلاتر (الشبكة) أو حقل
    الباسورد (صفحة الدخول) — بدل 9 ث ثابتة. حد أقصى 20 ث."""
    # 🔴 2026-09-06 12:21: بعد تسجيل دخول جديد شريط الفلاتر بيظهر **قبل** العدّاد
    #    بثواني، والأساس اتجهّز على شبكة لسه بتحمّل → baseline_failed. الدليل
    #    الصح هو **العدّاد نفسه** (أو 'too many records' = الشبكة حمّلت بس محتاجة
    #    فلتر) — مش مجرد ظهور الـchips.
    waited = 0
    while waited < max_ms:
        page.wait_for_timeout(500)
        waited += 500
        try:
            if page.locator("input[type=password]").count():
                return "login"
            if read_count(page) is not None or too_many(page):
                page.wait_for_timeout(1000)
                return "grid"
        except Exception:
            pass
    return "timeout"


def prepare_next(page, log=print):
    """بعد تسليم النتيجة (برّه وقت المستخدم): نرجع للشبكة ونرجّع الأساس،
    عشان المهمة الجاية تبدأ على طول من غير تحميل ولا تصليح فلاتر."""
    try:
        if not page.url.startswith(GRID_URL):
            page.goto(GRID_URL, wait_until="domcontentloaded", timeout=60000)
            _wait_grid_or_login(page)
        if is_authenticated(page):
            setup_baseline(page, log=log)
            return True
    except Exception as e:
        log("تجهيز الأساس للمهمة الجاية فشل: %s" % type(e).__name__)
    return False


def is_authenticated(page):
    try:
        if page.locator("input[type=password]").count() > 0:
            return False
        low = page.content().lower()
        return ("service request management" in low or "my requests" in low)
    except Exception:
        return False


def ensure_session(page, user, pwd, log=print):
    """يفتح الشبكة. لو الجلسة انتهت بيعمل دخول بالمعرّفات الحالية.

    ⚠️ #username / #password / #submit — مش "LOG IN" الإنجليزي القديم،
       صفحة الدخول اتغيّرت وبقت بالعربي.
    """
    # تحسين 2026-09-06: لو الصفحة أصلاً على الشبكة (الوكيل بيرجّعها بعد كل
    # مهمة) والجلسة شغّالة — مافيش إعادة تحميل ولا 9 ث انتظار.
    try:
        if page.url.startswith(GRID_URL) and is_authenticated(page) \
                and chip_exists(page, "Active"):
            return "reused"
    except Exception:
        pass
    page.goto(GRID_URL, wait_until="domcontentloaded", timeout=60000)
    _wait_grid_or_login(page)
    if is_authenticated(page):
        return "reused"
    log("الجلسة منتهية — بيسجّل دخول")
    page.goto("https://support.degypt.net/saw/", wait_until="domcontentloaded",
              timeout=60000)
    page.wait_for_selector("#password", timeout=30000)
    page.fill("#username", user)
    page.fill("#password", pwd)
    page.click("#submit")
    page.wait_for_timeout(5000)
    page.goto(GRID_URL, wait_until="domcontentloaded", timeout=60000)
    _wait_grid_or_login(page)
    if not is_authenticated(page):
        raise RuntimeError("login_failed")
    return "logged_in"


# ------------------------------------------------------------ فتح الشكوى
def first_row_id(page):
    """الـId بتاع أول صف — من عمود Id بالذات.

    🔴 SlickGrid بيرسم الخلايا اللي جوه الشاشة بس. بعد الـscroll لليمين
       (عشان نوصل Shipment No) خلية الـId مش موجودة في الـDOM أصلاً،
       وأول خلية مرسومة بتبقى Request number — فطلع 538345 بدل 3729639.
       الحل: نرجع بالـscroll للشمال، ونقرا الخلية اللي class بتاعها l{idx}.
    """
    page.evaluate("""() => {
      const vp=document.querySelector('.slick-viewport');
      const hv=document.querySelector('.slick-header-scroller,.slick-header');
      if(vp) vp.scrollLeft=0; if(hv) hv.scrollLeft=0;
    }""")
    page.wait_for_timeout(1500)
    return page.evaluate("""() => {
      const heads=[...document.querySelectorAll('.slick-header-column')];
      let idx=-1;
      heads.forEach((h,i)=>{const s=h.querySelector('.slick-column-name');
        if(s && s.innerText.trim()==='Id') idx=i;});
      const row=document.querySelector('.slick-row');
      if(!row) return null;
      if(idx>=0){
        const c=row.querySelector('.slick-cell.l'+idx);
        if(c){const m=(c.innerText||'').match(/\\d{5,9}/); if(m) return m[0];}
      }
      const a=row.querySelector('a[href*="/Request/"]');
      if(a){const m=a.getAttribute('href').match(/Request\\/(\\d+)/); if(m) return m[1];}
      return null;
    }""")

def _fields(page):
    """خريطة اسم الحقل -> قيمته من صفحة general."""
    return page.evaluate("""() => {
      const out = {};
      document.querySelectorAll('.field-container').forEach(fc => {
        const lab = fc.querySelector('.label-text');
        if (!lab) return;
        const name = (lab.innerText||'').trim();
        if (!name) return;
        let val = '';
        const inp = fc.querySelector('input,textarea');
        if (inp && inp.value) val = inp.value;
        if (!val) {
          const clone = fc.cloneNode(true);
          const lc = clone.querySelector('.label-container');
          if (lc) lc.remove();
          val = (clone.innerText||'').trim();
        }
        if (val) out[name] = val.replace(/\\s+/g, ' ').slice(0, 1200);
      });
      return out;
    }""")


def open_complaint(page, rid):
    """يفتح الشكوى ويستخرج حقول general."""
    page.goto("https://support.degypt.net/saw/Request/%s/general" % rid,
              wait_until="domcontentloaded", timeout=60000)
    # 🔴 انتظار على دليل مش وقت ثابت: 9 ث كانت كفاية محليًا وفشلت على الوكيل
    #    (الحقول رجعت فاضية والمناقشات كاملة). بنستنى ظهور حقل Creation Time
    #    بحد أقصى ~45 ث، وبعدها ثانية ونص عشان باقي الحقول تتملي.
    for _ in range(30):
        page.wait_for_timeout(1500)
        try:
            if page.evaluate("""() => [...document.querySelectorAll(
                  '.field-container .label-text')].some(
                  e => /Creation Time/i.test(e.innerText||''))"""):
                break
        except Exception:
            pass
    page.wait_for_timeout(1500)
    f = _fields(page)
    def g(*names):
        for n in names:
            for k, v in f.items():
                if k.strip().lower() == n.lower():
                    return v
        for n in names:
            for k, v in f.items():
                if n.lower() in k.strip().lower():
                    return v
        return ""
    desc = g("Description")
    if not desc:
        try:
            desc = page.evaluate("""() => {
              const e=document.querySelector('.cke_editable,[contenteditable=true]');
              return e ? (e.innerText||'').trim().slice(0,1500) : '';
            }""")
        except Exception:
            desc = ""
    return {
        "id": rid,
        "request_number": g("Request number", "Request num"),
        "creation_time": g("Creation Time", "Created time"),
        "assignment_group": g("Current assignment group"),
        "assignee": g("Assignee"),
        # 🔴 الحقل بيرجّع القيمة + قائمة الخيارات كلها ("In progress Status *
        #    Status * Ready In progress …") — بناخد اللي قبل كلمة Status بس.
        "status": re.split(r"\s+Status\b", g("Status"), 1)[0].strip(),
        "ticket_status": g("Ticket Status"),
        "title": g("Title"),
        "description": desc,
    }


# ------------------------------------------------------------ المناقشات
_TIME_RE = (
    r"[A-Z][a-z]+ \d{1,2},? \d{4}[, ]+\d{1,2}:\d{2}(?::\d{2})?\s*[APap]\.?[Mm]\.?",
    r"\d{1,2}/\d{1,2}/\d{2,4}[, ]+\d{1,2}:\d{2}(?::\d{2})?\s*(?:[APap]\.?[Mm]\.?)?",
)


def _when(lines):
    for ln in lines:
        for pat in _TIME_RE:
            m = re.search(pat, ln)
            if m:
                return m.group(0).strip()
    return ""


def extract_discussions(page, rid):
    """كل التعليقات بترتيب الصفحة — ومعاها الكاتب والتاريخ.

    ⚠️ ترتيب الصفحة هو مصدر الحقيقة (زي Smax_V11) — التواريخ بتتقري
       وبتتعرض، بس الترتيب مابيتعادش حسابه منها عشان التعليقات اللي
       من غير تاريخ ماتتبعترش.
    """
    page.goto("https://support.degypt.net/saw/Request/%s/discussions" % rid,
              wait_until="domcontentloaded", timeout=60000)
    # انتظار على دليل (2026-09-06): ظهور أول تعليق أو منسدلة Show — بدل 7 ث
    # ثابتة، بحد أقصى 20 ث. وبعدها ثانية ونص عشان الباقي يتملي.
    for _ in range(40):
        page.wait_for_timeout(500)
        try:
            if page.evaluate("""() => /Submitted by|Automatically generated|All discussions/i
                                    .test(document.body.innerText||'')"""):
                break
        except Exception:
            pass
    page.wait_for_timeout(1500)
    for _ in range(4):
        try:
            page.mouse.wheel(0, 2500)
        except Exception:
            pass
        page.wait_for_timeout(700)
    # 🔴 منقول من Smax_V11/scraper.py:time_ago_map — آخر تعليق بيتعرض
    #    نسبي («3 days ago») والتاريخ المطلق قاعد في data-original-title،
    #    وinner_text مابيشوفهوش. مقيس هناك: 34 من 34 شكوى.
    ago = {}
    try:
        for r in page.evaluate("""() => Array.from(document.querySelectorAll('[pl-time-ago]'))
              .map(el => ({shown:(el.textContent||'').trim(),
                           abs: el.getAttribute('data-original-title')||''}))""") or []:
            if r["shown"] and r["abs"] and r["shown"] != r["abs"]:
                ago.setdefault(r["shown"], []).append(r["abs"])
    except Exception:
        ago = {}
    ago_used = {}

    body = page.evaluate("() => document.body.innerText")
    if "Submitted by" not in body:
        return []
    parts = body.split("Submitted by")
    out = []
    SYS = re.compile(
        r"automatically generated|request has been assigned|loading discussions"
        r"|agent to agent|external service desk to agent", re.I)
    for i in range(len(parts) - 1):
        block = parts[i].strip()
        nxt = parts[i + 1].strip()
        if len(block) < 20:
            continue
        lines = [x.strip() for x in block.split("\n") if x.strip()]
        content = []
        for ln in reversed(lines):
            low = ln.lower()
            if "status update" in low or "follow up" in low:
                break
            if low in ("internal", "public") or "|" in ln:
                continue
            if "agent to agent" in low or "external service desk to agent" in low:
                continue
            content.insert(0, ln)
        text = "\n".join(content).strip()
        if not text or SYS.search(text):
            continue
        nl = [x.strip() for x in nxt.split("\n") if x.strip()]
        author = nl[0][:60] if nl else "Unknown"
        when = _when(nl[:3])
        if not when and ago:
            # نفس منطق resolve_time_ago: بالدور حسب موقع التعليق
            for ln in nl[:3]:
                for shown, vals in ago.items():
                    if shown in ln:
                        k = ago_used.get(shown, 0)
                        if k < len(vals):
                            ago_used[shown] = k + 1
                            when = vals[k]
                        break
                if when:
                    break
        # بادئة نوع الحوار مش جزء من التعليق
        #   "Agent to Agent - INTERNAL - " · "External service desk to  Agent - PUBLIC - "
        #   (المسافات والحروف بتختلف — بنمسك أي "… to Agent - X - " في الأول)
        text = re.sub(r"^\s*[A-Za-z ]{0,40}?\bto\s+Agent\s*-\s*(INTERNAL|PUBLIC)\s*-\s*",
                      "", text, flags=re.I).strip()
        out.append({"author": re.sub(r"\s+", " ", author),
                    "when": when,
                    "text": text[:1500]})
    return out

# ------------------------------------------------------------ سلّم البحث
def add_standard_filter(page, field, value):
    """فلتر من قائمة الفلاتر القياسية (زي National-ID)."""
    page.locator('a[title="Add filter"]').first.click()
    page.wait_for_timeout(SETTLE)
    page.locator("input.select2-input.select2-focused").first.fill(field)
    if not _pick(page, field):
        return None, "field_not_found"
    before = read_count(page)
    return _apply_value(page, value, before)


def request_forms(barcode, request_no=None):
    """صيغ رقم الطلب: من الملف لو موجود، ثم المشتقّة — عادي و "1" قدامها.

    ⚠️ الصيغة مش بتتحدد بالبادئة (تصحيح المستخدم) — بنجرّب الاتنين.
    """
    import re as _re
    out = []
    if request_no:
        out.append(str(request_no).strip())
    m = _re.match(r"^[A-Za-z]{2,4}(\d+)[A-Za-z]{2}$", str(barcode).strip())
    if m:
        d = m.group(1)
        out += [d.lstrip("0") or d, "1" + d]
    seen, uniq = set(), []
    for x in out:
        if x and x not in seen:
            seen.add(x); uniq.append(x)
    return uniq


def reapply(page, name):
    """OK تاني على فلتر موجود — بعد ما شيلنا فلتر تاني (الفلاتر AND)."""
    before = read_count(page)
    if open_chip_editor(page, name):
        _commit(page, before=before)
    return read_count(page)


def row_id_exact(page, column, value):
    """الـId بتاع الصف اللي خلية `column` فيه **تساوي** `value` بالظبط.

    🔴 مكتشف 2026-09-06: فلتر Request number بيطابق **بداية** الرقم مش كله —
       `61674` جاب شكوى رقمها `6167415544` (مقفولة من 2025) وكانت هتتسلّم
       للمستخدم كأنها بتاعته. فبنتحقق من الخلية نفسها قبل ما نقبل أي نتيجة.
    الشبكة بترسم الخلايا اللي جوه الشاشة بس، فبنقرا العمود بعد ما نزحلقه
    للشاشة، ونطابق الصف بالـtop بتاعه، ثم نرجع للشمال ونقرا الـId.
    """
    want = re.sub(r"\s+", "", str(value)).upper()
    box = _scroll_header_into_view(page, column)
    if not box:
        return None
    page.wait_for_timeout(600)
    tops = page.evaluate("""(a) => {
      const [name, want] = a;
      const heads=[...document.querySelectorAll('.slick-header-column')];
      let idx=-1;
      heads.forEach((h,i)=>{const s=h.querySelector('.slick-column-name');
        if(s && s.innerText.trim()===name) idx=i;});
      if(idx<0) return null;
      const out=[];
      document.querySelectorAll('.slick-row').forEach(r=>{
        const c=r.querySelector('.slick-cell.l'+idx);
        if(!c) return;
        const t=(c.innerText||'').replace(/\\s+/g,'').toUpperCase();
        if(t===want) out.push(r.style.top);
      });
      return out;
    }""", [column, want])
    if not tops:
        return None
    page.evaluate("""() => {
      const vp=document.querySelector('.slick-viewport');
      const h=document.querySelector('.slick-header-column');
      if(vp) vp.scrollLeft=0;
      for (let e = h && h.parentElement; e; e = e.parentElement) {
        if (e.scrollLeft > 0) e.scrollLeft = 0;
      }
    }""")
    page.wait_for_timeout(1200)
    return page.evaluate("""(tops) => {
      const heads=[...document.querySelectorAll('.slick-header-column')];
      let idx=-1;
      heads.forEach((h,i)=>{const s=h.querySelector('.slick-column-name');
        if(s && s.innerText.trim()==='Id') idx=i;});
      for (const r of document.querySelectorAll('.slick-row')) {
        if(!tops.includes(r.style.top)) continue;
        if(idx>=0){const c=r.querySelector('.slick-cell.l'+idx);
          if(c){const m=(c.innerText||'').match(/\\d{5,9}/); if(m) return m[0];}}
        const a=r.querySelector('a[href*="/Request/"]');
        if(a){const m=a.getAttribute('href').match(/Request\\/(\\d+)/); if(m) return m[1];}
      }
      return null;
    }""", tops)


def _hit(page, by, scope, tried, column=None, value=None):
    """نتيجة مقبولة = صف قيمته في العمود **تساوي** القيمة اللي بحثنا بيها."""
    if column and value:
        rid = row_id_exact(page, column, value)
        if not rid:
            tried[-1:] = [tried[-1] + " (مطابقة جزئية — مرفوضة)"] if tried else []
            return None
    else:
        rid = first_row_id(page)
        if not rid:
            return None
    return {"found": True, "id": rid, "found_by": by, "scope": scope,
            "tried": " ← ".join(tried)}


def run_search(page, barcode, request_no=None, national_id=None, log=print):
    """السلّم المؤكد من المستخدم (2026-09-05):

      ١. Shipment No + Egypt Post
      ٢. Shipment No شامل            (شيل الجروب بس)
      ─── الاتنين فشلوا = مشكلة في رقم الشحنة نفسه ───
      ٣. Request number شامل         (ضيفه الأول، ثم شيل فلتر الشحنة، ثم OK تاني)
      ٤. National-ID شامل            (ضيفه الأول، ثم شيل فلتر الطلب، ثم OK تاني)

    ⚠️ "ضيف الجديد قبل ما تشيل القديم" مش تفصيلة: لو شيلنا القديم الأول،
       الشبكة تقول 'too many records' وتختفي الأعمدة وزرار الفلتر معاها.
    """
    tried = []
    if not setup_baseline(page, log=log):
        # محاولة تانية بعد 5 ث — الحالة الوحيدة اللي شفناها: شبكة لسه بتحمّل
        # بعد تسجيل دخول جديد (2026-09-06 12:21).
        log("   الأساس فشل — محاولة تانية بعد 5 ث")
        page.wait_for_timeout(5000)
        if not setup_baseline(page, log=log):
            return {"found": False, "tried": "baseline_failed"}

    # ١
    cnt, how = filter_by_column(page, "Shipment No", barcode)
    tried.append("Shipment No (البريد)")
    log("  ١ Shipment No=%s | البريد -> %s (%s)" % (barcode, cnt, how))
    if how == "ok" and cnt and cnt > 0:
        h = _hit(page, "Shipment No", "egypt_post", tried, "Shipment No", barcode)
        if h: return h
    if how not in ("ok",):
        return {"found": False, "tried": " ← ".join(tried), "error": how}

    forms = request_forms(barcode, request_no)

    # ٢ → ٤ (Active=Yes)
    h = _global_ladder(page, barcode, forms, national_id, tried, log, "")
    if h:
        return h

    # ٥ الشكاوى المقفولة (قرار المستخدم 2026-09-06): Active=No ثم ٢→٤ تاني
    if set_active(page, "No"):
        tried.append("Active=No")
        log("  ٥ Active=No — بندوّر في المقفولة")
        h = _global_ladder(page, barcode, forms, national_id, tried, log, " · مقفولة")
        if h:
            h["closed"] = True
            return h
    else:
        log("  ٥ تعذّر تغيير Active إلى No")

    return {"found": False, "tried": " ← ".join(tried)}


def _apply_only(page, kind, name, value, tried, label, log):
    """يضيف/يعدّل فلتر واحد ويخلّيه **الوحيد** (مع Active) ثم OK تاني.

    القاعدة (المستخدم 2026-09-05): الفلتر الجديد يتضاف **قبل** ما القديم
    يتشال — لو شلنا القديم الأول الشبكة تقع على 'too many records' والأعمدة
    تختفي. kind = 'column' (Shipment No / Request number) أو 'standard'
    (National-ID). بيرجّع (count, how).
    """
    if kind == "column":
        cnt, how = filter_by_column(page, name, value)
    else:
        cnt, how = add_standard_filter(page, name, value)
    # اللي بيتعرض للمستخدم في «جرّبنا» مختصر — من غير رقم الخطوة
    short = re.sub(r"^[٠-٩0-9]+\s*", "", label).replace(" | شامل", " (شامل)")
    if not chip_exists(page, name):
        tried.append(short)
        log("  %s -> None (%s)" % (label, how))
        return None, how
    others = [n for n in chip_names(page)
              if n.lower() != "active" and not n.lower().startswith(name.lower())]
    for n in others:
        remove_chip(page, n)
    if others:
        cnt = reapply(page, name)
    tried.append(short)
    log("  %s -> %s (%s)" % (label, cnt, how))
    return cnt, how


def _global_ladder(page, barcode, forms, national_id, tried, log, tag):
    """الخطوات الشاملة ٢→٤ بنفس الترتيب — بتتنفّذ مرتين: Active=Yes ثم
    (لو فشلت كلها) Active=No للشكاوى المقفولة."""
    cnt, how = _apply_only(page, "column", "Shipment No", barcode, tried,
                           "٢ Shipment No=%s | شامل%s" % (barcode, tag), log)
    if cnt and cnt > 0:
        h = _hit(page, "Shipment No", "global", tried, "Shipment No", barcode)
        if h: return h
    for form in forms:
        cnt, how = _apply_only(page, "column", "Request number", form, tried,
                               "٣ Request number=%s | شامل%s" % (form, tag), log)
        if cnt and cnt > 0:
            h = _hit(page, "Request number=%s" % form, "global", tried,
                     "Request number", form)
            if h: return h
    if national_id:
        cnt, how = _apply_only(page, "standard", "National-ID", national_id, tried,
                               "٤ National-ID | شامل%s" % tag, log)
        if cnt and cnt > 0:
            h = _hit(page, "National-ID", "global", tried, "National-ID", national_id)
            if h: return h
    return None

def search_and_extract(page, barcode, request_no=None, national_id=None,
                       log=print):
    """السلّم + فتح الشكوى + الاستخراج الكامل."""
    hit = run_search(page, barcode, request_no, national_id, log=log)
    if not hit.get("found"):
        return hit
    data = open_complaint(page, hit["id"])
    comments = extract_discussions(page, hit["id"])
    last = comments[-1] if comments else None
    # قرار المستخدم 2026-09-05 (تعديل): آخر تعليق يظهر لوحده فوق **ويتكرر**
    # كآخر عنصر مرقّم في المناقشات عشان الترتيب الزمني يبقى كامل.
    rest = list(comments)
    data.update({
        "found": True,
        "closed": bool(hit.get("closed")),   # اتلقت بـActive=No (مقفولة)
        "found_by": hit["found_by"],
        "scope": hit["scope"],
        "tried": hit["tried"],
        "last_comment": last,
        "comments": rest,
        "comments_total": len(comments),
    })
    return data
