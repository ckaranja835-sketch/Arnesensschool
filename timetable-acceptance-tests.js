const {makeEnv}=require('./harness.js');
const assert=(c,m)=>{ if(!c){console.log('  FAIL:',m); process.exitCode=1;} else console.log('  ok  :',m); };
const {ctx,DATA,calls}=makeEnv();
const subs=[['MATH','Mathematics','MAT'],['ENG','English','ENG'],['KIS','Kiswahili','KIS'],['SCI','Integrated Science',''],['SST','Social Studies',''],['AGR','Agriculture','AGR'],['CA','Creative Arts',''],['PE','Physical Education',''],['PT','Pre-Technical Studies',''],['RE','Religious Education','']];
subs.forEach(([id,name,code])=>DATA.subjects.push({id,name,code}));
const lower=['Grade 4','Grade 5','Grade 6'].flatMap(l=>['A','B'].map(s=>({id:l+s,level:l,stream:s})));
const jss=['Grade 7','Grade 8','Grade 9'].flatMap(l=>['A','B'].map(s=>({id:l+s,level:l,stream:s})));
DATA.classes.push(...lower,...jss);
// realistic staffing: 3 teachers per subject per section (6 for Maths) + 2 SHARED teachers who teach Maths & Science in BOTH sections
for(const [sec,cls] of [['L',lower],['J',jss]]) for(const [sid] of subs){ const k=sid==='MATH'?6:3; for(let i=0;i<k;i++) DATA.teachers.push({id:`T${sec}${sid}${i}`,firstName:`T${sec}${sid}`,lastName:String(i),subjectIds:[sid],classIds:cls.map(c=>c.id)}); }
for(const id of ['SH1','SH2']) DATA.teachers.push({id,firstName:'Shared',lastName:id,subjectIds:['MATH','SCI'],classIds:[...lower,...jss].map(c=>c.id)});
const rulesFor=(division)=>{
  const R=[['MATH',3,1],['ENG',4,0],['KIS',3,0],['SCI',2,1],['SST',3,0],['AGR',2,1],['CA',2,0],['PE',2,0],['RE',2,0]];
  return R.map(([s,si,d])=>({id:'r'+division+s,division,classId:null,subjectId:s,singles:si,doubles:d}));
};
DATA.timetableRules.push(...rulesFor('Primary'),...rulesFor('JSS'));
// class-specific exception: Grade 5A = 4 singles + 3 doubles of Maths (10 periods) — the spec's example
DATA.timetableRules.push({id:'x5a',division:null,classId:'Grade 5A',subjectId:'MATH',singles:4,doubles:3});
const nDoubles=(cid)=>{const cl=ctx.getClass(cid);const b=ctx.ttClassBlocks(DATA.timetable.filter(t=>t.classId===cid),ctx.classDivision(cl));const per={};b.filter(x=>x.double).forEach(x=>per[x.day]=(per[x.day]||0)+1);return per;};

(async()=>{
console.log('TEST 1 — 3 doubles/week land on different days (200 runs, Lower Grade 5A)');
let worst=0, failures=0;
for(let i=0;i<200;i++){
  const ok=await ctx.autoGenerateTimetableForClass('Grade 5A',false);
  if(!ok){failures++;continue;}
  const per=nDoubles('Grade 5A'); worst=Math.max(worst,...Object.values(per));
  const mathDoubles=ctx.ttClassBlocks(DATA.timetable.filter(t=>t.classId==='Grade 5A'),'Primary').filter(b=>b.double&&b.subjectId==='MATH').length;
  if(mathDoubles!==3){failures++;}
}
assert(failures===0,'all 200 generations succeeded with exactly 3 MAT doubles ('+failures+' failures)');
assert(worst===1,'max doubles per day for the class = '+worst);
const days=Object.keys(nDoubles('Grade 5A')); console.log('   sample double days:',days.join(','));

console.log('TEST 2 — two doubles same day is rejected by manual editor');
const cid='Grade 5A';
// clear Monday and place a MAT double at slots 0-1
await ctx.ttBulkCommit([],DATA.timetable.filter(t=>t.classId===cid&&t.day==='Mon').map(t=>t.id));
let r=await ctx.ttApplyManualEdit(cid,'Mon',0,'MATH','TLMATH0','double'); assert(r.ok,'Monday MAT double accepted');
r=await ctx.ttApplyManualEdit(cid,'Mon',2,'ENG','TLENG0','double'); assert(r.error&&/already has a double lesson on Monday/.test(r.error),'Monday ENG double rejected: '+(r.error||'').slice(0,90));
assert(nDoubles(cid).Mon===1,'Monday still has exactly 1 double');
r=await ctx.ttApplyManualEdit(cid,'Mon',2,'ENG','TLENG0','single'); assert(r.ok,'ENG single on Monday accepted');

console.log('TEST 3 — double crossing a break rejected');
r=await ctx.ttApplyManualEdit(cid,'Tue',1,'ENG','TLENG0','double'); // slot1 (9:00-9:30) + slot2 (9:50) crosses short break
await ctx.ttBulkCommit([],DATA.timetable.filter(t=>t.classId===cid&&t.day==='Tue').map(t=>t.id));
r=await ctx.ttApplyManualEdit(cid,'Tue',1,'ENG','TLENG0','double'); assert(r.error&&/consecutive teaching periods/.test(r.error),'break-crossing double rejected: '+(r.error||'').slice(0,100));
r=await ctx.ttApplyManualEdit(cid,'Tue',7,'ENG','TLENG0','double'); assert(r.error,'double at last period of day rejected: '+(r.error||'').slice(0,80));
r=await ctx.ttApplyManualEdit(cid,'Tue',5,'ENG','TLENG0','double'); assert(r.error&&/consecutive/.test(r.error),'double across lunch (slot5+6) rejected');

console.log('TEST 4 — visual: double = ONE box spanning two periods');
ctx.loadTTScheduleForDivision('Primary');
const grid=ctx.ttGridTableHtml('class',cid,true);
const monRow=grid.split('<tr><td class="tt-time-col">').find(x=>x.startsWith('Monday'));
assert((monRow.match(/colspan="2"/g)||[]).length===1,'Monday row has exactly one colspan=2 cell');
assert((monRow.match(/DOUBLE/g)||[]).length===1,'exactly one DOUBLE block, not two MAT boxes');
const monMat=(monRow.match(/>MAT</g)||[]).length; assert(monMat===1,'MAT drawn once in Monday row ('+monMat+')');
const SL=require('vm').runInContext('TT_SCHEDULE.length',ctx); const cells=(monRow.match(/<td /g)||[]).length;
assert(cells===SL-1,'Monday has '+cells+' cells for '+SL+' schedule rows (one row covered by the double)');

console.log('TEST 5 — abbreviations');
assert(ctx.ttSubjectAbbr({id:'x',name:'Mathematics',code:''})==='MAT','Mathematics → MAT (no code)');
assert(ctx.ttSubjectAbbr({id:'y',name:'Mathematics',code:'MTH'})==='MTH','existing code MTH is used');
assert(ctx.ttSubjectAbbr({id:'z',name:'Integrated Science',code:''})==='INT SCI','Integrated Science → INT SCI');
assert(ctx.ttSubjectAbbr({id:'w',name:'Pre-Technical Studies',code:''})==='PRE-TECH','Pre-Technical → PRE-TECH');
assert(ctx.ttSubjectAbbr({id:'v',name:'Social Studies',code:''})==='SST','Social Studies → SST');
assert(ctx.ttSubjectAbbr({id:'u',name:'Physical Education',code:''})==='PE','Physical Education → PE');
assert(!/MATHEMATICS/.test(grid),'grid never prints MATHEMATICS');

console.log('TEST 6 — section-specific errors');
// Make ONE JSS class impossible: Grade 8A needs 6 doubles of Science (max 1 per day => 5)
DATA.timetableRules.push({id:'x8a',division:null,classId:'Grade 8A',subjectId:'SCI',singles:0,doubles:6});
const before=JSON.stringify(DATA.timetable.filter(t=>t.classId.startsWith('Grade 4')||t.classId.startsWith('Grade 5')||t.classId.startsWith('Grade 6')).map(t=>t.id).sort());
await ctx.autoGenerateMasterTimetable(false,'Primary'); // Lower only — must succeed
const okLower=calls.toasts.length; 
let resL=await ctx.ttRunGeneration({scope:[{division:'Primary',classIds:lower.map(c=>c.id)}],fromButton:false});
assert(resL.ok && resL.errors.length===0,'Lower-only generation: no errors even though JSS has a bad rule');
let resJ=await ctx.ttRunGeneration({scope:[{division:'JSS',classIds:jss.map(c=>c.id)}],fromButton:false});
assert(!resJ.ok && resJ.errors.length>0 && resJ.errors.every(e=>e.section==='JSS'),'JSS-only generation: '+resJ.errors.length+' error(s), all tagged JSS: "'+resJ.errors[0].section+' — Grade 8A — '+ctx.getSubject(resJ.errors[0].subjectId).name+' — '+resJ.errors[0].reason.slice(0,80)+'…"');
let resW=await ctx.ttRunGeneration({scope:[{division:'Primary',classIds:lower.map(c=>c.id)},{division:'JSS',classIds:jss.map(c=>c.id)}],fromButton:false});
assert(!resW.ok && resW.errors.every(e=>e.section==='JSS'),'Whole School: the JSS problem is NOT reported as a Lower error');
ctx.openGenerationErrorsModal('t',resW.errors,{multi:true});
assert(/JSS ERRORS/.test(calls.modals.at(-1)[1]) && !/LOWER ERRORS/.test(calls.modals.at(-1)[1]),'modal shows a JSS ERRORS group only');
DATA.timetableRules.pop();

console.log('TEST 7 — teacher conflicts across Lower/JSS (shared teachers + real clock times)');
const w=await ctx.ttRunGeneration({scope:[{division:'Primary',classIds:lower.map(c=>c.id)},{division:'JSS',classIds:jss.map(c=>c.id)}],fromButton:false});
assert(w.ok,'whole-school generation succeeded ('+w.errors.length+' errors)'+(w.errors[0]?' e.g. '+w.errors[0].reason.slice(0,120):''));
const probs=ctx.ttValidateEntries(DATA.timetable).filter(p=>p.type==='teacher-conflict');
assert(probs.length===0,'saved timetable has zero teacher conflicts ('+probs.length+')');
const secOf=id=>ctx.classDivision(ctx.getClass(id));
const sharedBoth=['SH1','SH2'].filter(t=>{const d=new Set(DATA.timetable.filter(e=>e.teacherId===t).map(e=>secOf(e.classId))); return d.size===2;});
console.log('   shared teachers used in BOTH sections:',sharedBoth.join(',')||'none');
// manual edit: put a shared teacher into a JSS class at a time that overlaps his Lower lesson (Lower L3 9:50–10:25 vs JSS L3 10:00–10:40)
const victim=DATA.timetable.find(e=>e.teacherId==='SH1'&&secOf(e.classId)==='Primary'&&e.slot===2);
if(victim){
  const tgt='Grade 8B'; await ctx.ttBulkCommit([],DATA.timetable.filter(t=>t.classId===tgt&&t.day===victim.day).map(t=>t.id));
  const rr=await ctx.ttApplyManualEdit(tgt,victim.day,2,'SCI','SH1','single');
  assert(rr.error&&/already teaching/.test(rr.error),'cross-section overlap blocked by manual editor: '+(rr.error||'').slice(0,110));
} else console.log('   (no SH1 lesson at Lower L3 in this random run — skipped)');
console.log('TEST 8 — regenerate JSS must not touch Lower');
await ctx.ttRunGeneration({scope:[{division:'Primary',classIds:lower.map(c=>c.id)}],fromButton:false});
const snap=JSON.stringify(DATA.timetable.filter(t=>lower.some(c=>c.id===t.classId)).map(t=>[t.id,t.classId,t.day,t.slot,t.subjectId,t.teacherId,t.dbl]).sort());
const rj=await ctx.ttRunGeneration({scope:[{division:'JSS',classIds:jss.map(c=>c.id)}],fromButton:false});
const after=JSON.stringify(DATA.timetable.filter(t=>lower.some(c=>c.id===t.classId)).map(t=>[t.id,t.classId,t.day,t.slot,t.subjectId,t.teacherId,t.dbl]).sort());
assert(rj.ok,'JSS regeneration succeeded'); assert(snap===after,'Lower timetable byte-for-byte unchanged after regenerating JSS');
const one=await ctx.autoGenerateTimetableForClass('Grade 7A',false);
assert(one && JSON.stringify(DATA.timetable.filter(t=>lower.some(c=>c.id===t.classId)).map(t=>[t.id,t.classId,t.day,t.slot,t.subjectId,t.teacherId,t.dbl]).sort())===snap,'single-class regeneration also leaves Lower untouched');
const cnt=c=>DATA.timetable.filter(t=>t.classId===c).length; assert(cnt('Grade 7B')>0,'other JSS classes keep their timetable when one class is regenerated');

console.log('TEST 9 — speed / batching');
calls.bulk=0; calls.post.length=0;
const t0=Date.now();
const big=await ctx.ttRunGeneration({scope:[{division:'Primary',classIds:lower.map(c=>c.id)},{division:'JSS',classIds:jss.map(c=>c.id)}],fromButton:false});
console.log('   12 classes, whole school:',Date.now()-t0,'ms; ok:',big.ok,'; bulk commits:',calls.bulk,'; server requests:',calls.post.length,'; entries saved:',big.saved);
assert(calls.bulk===1&&calls.post.length===1,'exactly ONE batched save / ONE server request for the whole run');
const allProbs=ctx.ttValidateEntries(DATA.timetable);
assert(allProbs.length===0,'final saved school timetable passes the full hard-constraint validator ('+allProbs.length+' problems)');
const perDay={}; DATA.classes.forEach(c=>{ const b=ctx.ttClassBlocks(DATA.timetable.filter(t=>t.classId===c.id),ctx.classDivision(c)); b.filter(x=>x.double).forEach(x=>{const k=c.id+x.day; perDay[k]=(perDay[k]||0)+1;}); });
assert(Object.values(perDay).every(v=>v===1),'every class/day has at most 1 double across the whole school');
})();
