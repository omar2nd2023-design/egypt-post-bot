/**
 * اختبارات بوّابة البحث في الشكاوى (SMAX) — القاعدة اتغيّرت 2026-09-09.
 *
 * القاعدة الجديدة: **عمر الطلب** هو الحكم، ومصدره الأول هو **الرحلة الحيّة**
 * (حدث «تم تسجيل الطلب»)، وملفاتنا احتياطي. قبل كده «مش في ملفاتنا» كان
 * بينهي الموضوع قبل ما العمر يتحسب أصلًا.
 *
 * مافيش شبكة ولا توكن هنا — بنستورد الدوال من worker.js كنص.
 *
 *   node test_smax_gate.mjs
 */
import { readFileSync } from 'node:fs';

const SRC = new URL('./src/worker.js', import.meta.url);
let code = readFileSync(SRC, 'utf8');
code = code.replace(/^export default \{[\s\S]*$/m, '');
code += `
export { smaxWorthIt, smaxSkipTail, startDate, orderAge, RECENT_DAYS };
`;
const M = await import(
  'data:text/javascript;base64,' + Buffer.from(code, 'utf8').toString('base64'));

let FAILED = 0;
function check(label, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) FAILED++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}` + (ok ? '' : `   got=${JSON.stringify(got)} want=${JSON.stringify(want)}`));
}
function has(label, text, part) {
  const ok = String(text).includes(part);
  if (!ok) FAILED++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${label}` + (ok ? '' : `   مالقيناش «${part}» في: ${text}`));
}

/** رحلة حيّة بتاريخ تسجيل من كام يوم فاتوا */
const ago = (days, h = '12:00:00') => {
  const d = new Date(Date.now() - days * 86400e3);
  return `${d.toISOString().slice(0, 10)} ${h}`;
};
const journey = (days) => ({
  records: [
    { ItemStatus: 'تم استلام الشحنه من الجهة', EventDateAndTime: ago(days, '16:14:06') },
    { ItemStatus: 'جاري التجهيز', EventDateAndTime: ago(days, '15:07:34') },
    { ItemStatus: 'تم تسجيل الطلب', EventDateAndTime: ago(days, '12:49:01') },
  ],
});

console.log('\n=== مصدر التاريخ ===');
check('تاريخ التسجيل بيتقري من الرحلة',
  M.startDate(journey(3)), ago(3).slice(0, 10));
check('لو الحالة اتسمّت غير كده بناخد أقدم حدث',
  M.startDate({ records: [
    { ItemStatus: 'حاجة جديدة', EventDateAndTime: ago(1, '10:00:00') },
    { ItemStatus: 'حاجة أقدم', EventDateAndTime: ago(9, '10:00:00') }] }),
  ago(9).slice(0, 10));
check('مافيش رحلة → تاريخ ملفاتنا',
  M.orderAge({ r: ago(20).slice(0, 10) }, null)?.date, ago(20).slice(0, 10));
check('الرحلة ليها الأولوية على ملفاتنا',
  M.orderAge({ r: ago(40).slice(0, 10) }, journey(2))?.date, ago(2).slice(0, 10));
check('أحداث البالون (مصفوفة events) بتشتغل برضه',
  M.startDate([{ status: 'تم تسجيل الطلب', time: ago(7, '09:00:00') }]),
  ago(7).slice(0, 10));

console.log('\n=== القرار: نبحث ولا لأ ===');
check('الحد لسه 5 أيام', M.RECENT_DAYS, 5);
check('اتسجّل النهاردة + مش في ملفاتنا → مافيش بحث',
  M.smaxWorthIt(null, journey(0)), false);
check('عمره 5 أيام بالظبط → مافيش بحث (البحث من السادس)',
  M.smaxWorthIt(null, journey(5)), false);
check('عمره 6 أيام + مش في ملفاتنا → **نبحث** (ده اللي اتغيّر)',
  M.smaxWorthIt(null, journey(6)), true);
check('عمره 10 أيام + مش في ملفاتنا → نبحث',
  M.smaxWorthIt(null, journey(10)), true);
check('في ملفاتنا وقديم → نبحث (زي الأول)',
  M.smaxWorthIt({ r: ago(30).slice(0, 10) }, null), true);
check('في ملفاتنا وجديد → مافيش بحث (زي الأول)',
  M.smaxWorthIt({ r: ago(1).slice(0, 10) }, null), false);
check('لا ملفات ولا رحلة → مافيش بحث',
  M.smaxWorthIt(null, null), false);
check('في ملفاتنا بتاريخ مش مقروء → نبحث (السلوك القديم محفوظ)',
  M.smaxWorthIt({ r: '' }, null), true);

console.log('\n=== نص الرد ===');
const t0 = M.smaxSkipTail(null, journey(0));
has('الرد بيقول تاريخ التسجيل', t0, ago(0).slice(0, 10));
has('الرد بيقول العمر', t0, 'عمره 0 يوم');
has('الرد بيقول يرجع بعد كام يوم', t0, 'بعد 6 يوم');
has('مافيش رحلة ولا ملفات → الرقم غلط غالبًا',
  M.smaxSkipTail(null, null), 'مالهاش رحلة عند البريد');
has('عمره 5 أيام → لسه بدري مش «رقم غلط»',
  M.smaxSkipTail(null, journey(5)), 'بعد 1 يوم');

console.log(FAILED ? `\n!!! فشل ${FAILED} فحص\n` : '\n✔ كل الفحوص نجحت\n');
process.exit(FAILED ? 1 : 0);
