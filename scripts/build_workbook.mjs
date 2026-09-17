import fs from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import {Workbook, SpreadsheetFile} from '@oai/artifact-tool';

const root=fileURLToPath(new URL('../',import.meta.url)).replace(/\/$/,'');
const out=`${root}/output/spreadsheet`;
const qa=`${root}/output/workbook_previews`;
await fs.mkdir(out,{recursive:true});
await fs.mkdir(qa,{recursive:true});
const parse=async path=>JSON.parse(await fs.readFile(path,'utf8'));
const lines=async path=>(await fs.readFile(path,'utf8')).trim().split('\n').map(JSON.parse);
const [publicSummary,rows,ops,opRows,scaleRows,sources]=await Promise.all([
  parse(`${root}/results/public/summary.json`),lines(`${root}/results/public/rows.jsonl`),
  parse(`${root}/results/operations/summary.json`),lines(`${root}/results/operations/task_rows.jsonl`),
  lines(`${root}/results/operations/scaling_rows.jsonl`),parse(`${root}/research/sources.json`)
]);
const wb=Workbook.create();
const summary=wb.worksheets.add('Summary');
const queries=wb.worksheets.add('Queries');
const operations=wb.worksheets.add('Operations');
const scale=wb.worksheets.add('Scaling');
const refs=wb.worksheets.add('Sources');
function base(sheet,range,title){
  sheet.showGridLines=false;
  sheet.getRange(range).format.font={name:'Arial',size:10,color:'#252525'};
  sheet.getRange(range).format.verticalAlignment='center';
  sheet.getRange(range).format.rowHeight=21;
  sheet.getRange('A2').values=[[title]];
  sheet.getRange('A2').format.font={name:'Arial',size:15,bold:true,color:'#222222'};
  sheet.getRange('A3:H3').format.borders={bottom:{style:'thin',color:'#B7BCC2'}};
}
function head(sheet,range){
  sheet.getRange(range).format={fill:'#343A40',font:{name:'Arial',size:10,bold:true,color:'#FFFFFF'},wrapText:true,horizontalAlignment:'center',verticalAlignment:'center',rowHeight:34};
}
function number(sheet,range,format='0.0%'){
  sheet.getRange(range).setNumberFormat(format);
  sheet.getRange(range).format.horizontalAlignment='right';
}
function widths(sheet,values){
  for(let i=0;i<values.length;i++)sheet.getRangeByIndexes(0,i,1,1).format.columnWidth=values[i];
}
const qFields=['dataset','question_id','category','cluster','method','corpus_messages','gold_count','recall_8','recall_20','all_gold_8','session_recall_8','packed_recall','packed_all_gold','packed_units','search_ms','pack_ms'];
base(queries,`A1:P${rows.length+5}`,'Public retrieval observations');
queries.getRange('A4:P4').values=[['Dataset','Question ID','Category','Cluster','Method','Corpus messages','Gold messages','Recall@8','Recall@20','All gold@8','Session recall@8 messages','Packed recall','Packed all gold','Estimated input units','Search ms','Packing ms']];
queries.getRange(`A5:P${rows.length+4}`).values=rows.map(row=>qFields.map(key=>row[key]??null));
head(queries,'A4:P4');
number(queries,`H5:M${rows.length+4}`);
number(queries,`F5:G${rows.length+4}`,'#,##0');
number(queries,`N5:N${rows.length+4}`,'#,##0');
number(queries,`O5:P${rows.length+4}`,'0.000');
widths(queries,[23,39,30,34,15,15,14,13,13,13,18,14,14,18,14,14]);
queries.freezePanes.freezeRows(4);
queries.tables.add(`A4:P${rows.length+4}`,true,'PublicQueries');

base(summary,'A1:J41','CMP research measurements');
widths(summary,[23,30,11,14,14,15,18,16,16,16]);
summary.getRange('A5:J5').values=[['Dataset','Method','Questions','Recall@8','Recall@20','All gold@8','Session recall@8 messages','Packed recall','Packed all gold','Mean input units']];
head(summary,'A5:J5');
const groups=publicSummary.groups.filter(g=>g.category==='all').sort((a,b)=>a.dataset.localeCompare(b.dataset)||['recency','overlap','bm25'].indexOf(a.method)-['recency','overlap','bm25'].indexOf(b.method));
const end=rows.length+4;
for(let i=0;i<groups.length;i++){
  const r=6+i,g=groups[i];
  summary.getRange(`A${r}:B${r}`).values=[[g.dataset,g.method]];
  summary.getRange(`C${r}`).formulas=[[`=COUNTIFS('Queries'!$A$5:$A$${end},A${r},'Queries'!$E$5:$E$${end},B${r})`]];
  const rawCols=['H','I','J','K','L','M','N'];
  summary.getRange(`D${r}:J${r}`).formulas=[rawCols.map(c=>`=AVERAGEIFS('Queries'!$${c}$5:$${c}$${end},'Queries'!$A$5:$A$${end},A${r},'Queries'!$E$5:$E$${end},B${r})`)];
}
number(summary,'D6:I11');number(summary,'C6:C11','#,##0');number(summary,'J6:J11','#,##0.0');
summary.getRange('A13').values=[['Recall is annotated-message recovery, not generated-answer accuracy.']];
summary.getRange('A13').format.font={name:'Arial',size:10,italic:true,color:'#4A4A4A'};
summary.getRange('A15:F15').values=[['Dataset','Paired comparison','Delta, pp','95% low, pp','95% high, pp','Clusters']];
head(summary,'A15:F15');
summary.getRange('A16:F19').values=publicSummary.paired_differences.map(r=>[r.dataset,r.comparison.replace('bm25_minus_','BM25 - '),r.delta*100,r.ci95[0]*100,r.ci95[1]*100,r.clusters]);
number(summary,'C16:E19','0.00');number(summary,'F16:F19','#,##0');
summary.getRange('A21').values=[['Bootstrap: 2,000 draws. LoCoMo clusters by conversation; LongMemEval by question.']];
summary.getRange('A21').format.font={name:'Arial',size:10,italic:true,color:'#4A4A4A'};
summary.getRange('A24:E24').values=[['Dataset','BM25 category','Questions','Recall@8','Packed recall']];head(summary,'A24:E24');
const cats=publicSummary.groups.filter(g=>g.method==='bm25'&&g.category!=='all');
for(let i=0;i<cats.length;i++){
  const r=25+i,g=cats[i];
  summary.getRange(`A${r}:B${r}`).values=[[g.dataset,g.category]];
  summary.getRange(`C${r}`).formulas=[[`=COUNTIFS('Queries'!$A$5:$A$${end},A${r},'Queries'!$C$5:$C$${end},B${r},'Queries'!$E$5:$E$${end},"bm25")`]];
  summary.getRange(`D${r}:E${r}`).formulas=[['H','L'].map(c=>`=AVERAGEIFS('Queries'!$${c}$5:$${c}$${end},'Queries'!$A$5:$A$${end},A${r},'Queries'!$C$5:$C$${end},B${r},'Queries'!$E$5:$E$${end},"bm25")`)];
}
number(summary,`D25:E${24+cats.length}`);number(summary,`C25:C${24+cats.length}`,'#,##0');

base(operations,'A1:M341','Task-state observations');
widths(operations,[11,30,14,17,14,19,19,18,13,20,19,16,14]);
const opFields=['seed','category','answerable','resolution','resolved','correct_resolution','false_resolution','state_unchanged','success','latest_revision_present','unrelated_evidence','budget_units','budget_ok'];
operations.getRange('A5:F5').values=[['Condition','Probes','Resolved rate','Correct resolutions','False resolutions','State unchanged']];head(operations,'A5:F5');
const categories=['explicit_id','registered_alias','unregistered_paraphrase','ambiguous_alias','absent_id'];
for(let i=0;i<categories.length;i++){
  const r=6+i;
  operations.getRange(`A${r}`).values=[[categories[i]]];
  operations.getRange(`B${r}`).formulas=[[`=COUNTIFS($B$18:$B$337,A${r})`]];
  operations.getRange(`C${r}:F${r}`).formulas=[['E','F','G','H'].map(c=>`=COUNTIFS($B$18:$B$337,A${r},$${c}$18:$${c}$337,1)/B${r}`)];
}
// The condition labels need the two left columns' combined visual width.
operations.getRange('A1:A341').format.columnWidth=31;
operations.getRange('B1:B341').format.columnWidth=30;
number(operations,'C6:F10');
operations.getRange('A12').values=[['Unregistered paraphrases are answerable misses: 0/40 resolved.']];
operations.getRange('A14').values=[['Raw probes below: missing fields are blank, not zero. Boolean outcomes are 0/1.']];
operations.getRange('A17:M17').values=[['Seed','Condition','Answerable','Resolution','Resolved','Correct resolution','False resolution','State unchanged','Snapshot success','Latest revision present','Unrelated messages','Input units','Budget satisfied']];head(operations,'A17:M17');
operations.getRange('A18:M337').values=opRows.map(r=>opFields.map(k=>typeof r[k]==='boolean'?Number(r[k]):r[k]??null));
number(operations,'L18:L337','#,##0');
operations.freezePanes.freezeRows(17);
operations.tables.add('A17:M337',true,'TaskProbes');

base(scale,'A1:G318','Indexed search scaling');
widths(scale,[17,18,13,16,17,20,22]);
scale.getRange('A5:G5').values=[['Messages','Query kind','Queries','Median ms','Sampled p95 ms','Bulk indexing sec','Database + journals MB']];head(scale,'A5:G5');
const sortedScale=[...scaleRows].sort((a,b)=>a.size-b.size||a.query_kind.localeCompare(b.query_kind)||a.query_index-b.query_index);
scale.getRange('A17:E17').values=[['Messages','Query kind','Query index','Latency ms','Hits']];head(scale,'A17:E17');
scale.getRange('A18:E317').values=sortedScale.map(r=>[r.size,r.query_kind,r.query_index,r.query_ms,r.hits]);
for(let i=0;i<ops.scaling.length;i++){
  const g=ops.scaling[i],r=6+i;
  const first=18+sortedScale.findIndex(x=>x.size===g.messages&&x.query_kind===g.query_kind);
  const last=first+49;
  scale.getRange(`A${r}:B${r}`).values=[[g.messages,g.query_kind]];
  scale.getRange(`C${r}:E${r}`).formulas=[[`=COUNT(D${first}:D${last})`,`=MEDIAN(D${first}:D${last})`,`=SMALL(D${first}:D${last},47)`]];
  scale.getRange(`F${r}:G${r}`).values=[[g.ingest_seconds,g.database_and_journal_bytes/1e6]];
}
number(scale,'D6:F11','0.000');number(scale,'G6:G11','0.00');number(scale,'A6:A11','#,##0');number(scale,'D18:D317','0.000');
scale.getRange('A13').values=[['Each query kind has 50 samples; p95 is the 47th ordered observation.']];
scale.getRange('A14').values=[['Synthetic local corpus. Timings exclude generation and network latency.']];
scale.freezePanes.freezeRows(17);scale.tables.add('A17:E317',true,'SearchSamples');

base(refs,'A1:E27','Sources and measurement provenance');
widths(refs,[7,65,34,73,55]);
refs.getRange('A5:E5').values=[['ID','Source','Date or revision','URL or access note','Use']];head(refs,'A5:E5');
const sourceRows=sources.map(s=>[s.id,`${s.authors.replace(/\.$/,'')}. ${s.title}`,s.date,s.url,s.use]);
sourceRows.push([16,'CMP public retrieval observations','Release 0.3.0rc1','Accompanying archive: results/public/rows.jsonl','Raw 5,991 rows; question-level metrics and source evidence identifiers.']);
sourceRows.push([17,'CMP operational and scaling observations','Release 0.3.0rc1','Accompanying archive: results/operations/','320 task probes and 300 timed searches.']);
sourceRows.push([18,'Dataset exclusions and unit definitions','Release 0.3.0rc1','Accompanying archive: research/PROTOCOL.md and research/SOURCES.md','LongMemEval: 470 scored; LoCoMo: 1,527 scored. No model calls.']);
refs.getRange(`A6:E${5+sourceRows.length}`).values=sourceRows;
refs.getRange(`A6:E${5+sourceRows.length}`).format.wrapText=true;
refs.getRange(`A6:E${5+sourceRows.length}`).format.rowHeight=57;
for(let i=0;i<sources.length;i++){
  const url=sources[i].url;
  refs.getRange(`D${6+i}`).values=[[url]];
}
refs.freezePanes.freezeRows(5);

wb.recalculate();
const scheck=await wb.inspect({kind:'table',range:'Summary!A5:J11',include:'values,formulas',tableMaxRows:8,tableMaxCols:10,maxChars:4500});
console.log(scheck.ndjson);
const opValues=operations.getRange('B6:F10').values;
for(let i=0;i<categories.length;i++){
 const observations=opRows.filter(row=>row.category===categories[i]);
 const expected=[observations.length,...['resolved','correct_resolution','false_resolution','state_unchanged'].map(key=>observations.reduce((sum,row)=>sum+Number(row[key]),0)/observations.length)];
 for(let j=0;j<expected.length;j++)if(typeof opValues[i][j]!=='number'||Math.abs(opValues[i][j]-expected[j])>1e-12)throw new Error('Operational formulas do not reconcile '+i+','+j);
}
for(const sheet of [summary,queries,operations,scale,refs]){
 for(const row of sheet.getUsedRange().values)for(const value of row)if(typeof value==='string'&&/not a function|not implemented|#(?:REF!|VALUE!|DIV\/0!|NAME\?|NUM!|N\/A)/.test(value))throw new Error('Calculation error: '+value);
}
const vals=summary.getRange('A6:J11').values;
for(let i=0;i<groups.length;i++){
  if(Math.abs(vals[i][3]-groups[i].recall_8)>1e-10 || vals[i][2]!==groups[i].n)throw new Error('Summary does not reconcile');
}
const scaleVals=scale.getRange('D6:E11').values;
for(let i=0;i<ops.scaling.length;i++)if(Math.abs(scaleVals[i][0]-ops.scaling[i].median_ms)>1e-9||Math.abs(scaleVals[i][1]-ops.scaling[i].p95_ms)>1e-9)throw new Error('Scaling does not reconcile');
// Validate formula response to an edited observation, then restore the source.
const original=queries.getRange('H5').values[0][0];
const selectedGroup=groups.findIndex(g=>g.dataset===rows[0].dataset&&g.method===rows[0].method);
queries.getRange('H5').values=[[original+.25]];
const changed=summary.getRange(`D${6+selectedGroup}`).values[0][0];
if(Math.abs(changed-(groups[selectedGroup].recall_8+.25/groups[selectedGroup].n))>1e-9)throw new Error('Edited input did not recalculate');
queries.getRange('H5').values=[[original]];
wb.recalculate();
const errors=await wb.inspect({kind:'match',searchTerm:'#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A|#NUM!|#NULL!|#SPILL!|#CALC!',options:{useRegex:true,maxResults:30},summary:'Formula errors'});
console.log(errors.ndjson);
for(const [sheetName,range,name] of [['Summary','A1:J35','summary'],['Queries','A1:I12','queries'],['Operations','A1:F25','operations'],['Scaling','A1:G25','scaling'],['Sources','A1:D12','sources']]){
  const preview=await wb.render({sheetName,range,scale:1.2,format:'png'});
  await fs.writeFile(`${qa}/${name}.png`,new Uint8Array(await preview.arrayBuffer()));
}
const file=await SpreadsheetFile.exportXlsx(wb);
await file.save(`${out}/CMP_Research_Data.xlsx`);
console.log(JSON.stringify({output:`${out}/CMP_Research_Data.xlsx`,queryRows:rows.length,taskRows:opRows.length,scalingRows:scaleRows.length,validated:true}));
