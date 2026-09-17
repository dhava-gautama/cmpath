"""Render paper figures from the actual measurements; no manual chart values."""
import json
from pathlib import Path
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import PercentFormatter, ScalarFormatter

root = Path(__file__).resolve().parents[1]
public = json.loads((root/"results/public/summary.json").read_text())
operations = json.loads((root/"results/operations/summary.json").read_text())
out = root/"figures"
out.mkdir(exist_ok=True)
plt.rcParams.update({"font.family":"DejaVu Sans","font.size":9,"axes.spines.top":False,
                     "axes.spines.right":False,"axes.titleweight":"bold","axes.labelcolor":"#303030",
                     "text.color":"#202020","axes.edgecolor":"#aaaaaa","savefig.facecolor":"white"})
colors = {"recency":"#b7bdc4","overlap":"#676f79","bm25":"#275c85"}
labels = {"recency":"Recency","overlap":"Token overlap","bm25":"CMP BM25"}

def save(fig,name):
    fig.savefig(out/f"{name}.png",dpi=220,bbox_inches="tight")
    fig.savefig(out/f"{name}.pdf",bbox_inches="tight")
    plt.close(fig)

fig,axes = plt.subplots(1,2,figsize=(7.15,3.0),layout="constrained")
for ax,dataset,title in zip(axes,["longmemeval_s","locomo10"],["LongMemEval-S","LoCoMo-10"]):
    groups = {r["method"]:r for r in public["groups"] if r["dataset"]==dataset and r["category"]=="all"}
    for index,method in enumerate(["bm25","overlap","recency"]):
        value = groups[method]["recall_8"]
        ax.barh(index,value,color=colors[method],height=.58)
        ax.text(value+.025,index,f"{value:.1%}",va="center",fontsize=9)
    ax.set_yticks(range(3),[labels[m] for m in ["bm25","overlap","recency"]])
    ax.invert_yaxis()
    ax.set_xlim(0,1.05)
    ax.set_xticks([0,.25,.5,.75,1])
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_title(f"{title} (n = {groups['bm25']['n']:,})",fontsize=10,pad=12)
    ax.set_xlabel("Mean message recall@8")
    ax.set_axisbelow(True)
    ax.grid(axis="x",alpha=.16)
save(fig,"public_recall")

fig,axes = plt.subplots(1,2,figsize=(7.15,3.5),layout="constrained")
names = {"single-session-user":"User fact","single-session-assistant":"Assistant fact",
         "single-session-preference":"Preference","knowledge-update":"Knowledge update",
         "multi-session":"Multi-session","temporal-reasoning":"Temporal reasoning"}
for ax,dataset,title in zip(axes,["longmemeval_s","locomo10"],["LongMemEval-S","LoCoMo-10"]):
    groups = [r for r in public["groups"] if r["dataset"]==dataset and r["method"]=="bm25" and r["category"]!="all"]
    groups.sort(key=lambda r:r["recall_8"],reverse=True)
    for index,row in enumerate(groups):
        value = row["recall_8"]
        ax.barh(index,value,color=colors["bm25"],height=.6)
        ax.text(value+.018,index,f"{value:.1%}",va="center",fontsize=8)
    ax.set_yticks(range(len(groups)),[f"{names.get(r['category'],'Category '+r['category'])} ({r['n']})" for r in groups],fontsize=8)
    ax.invert_yaxis()
    ax.set_xlim(0,1.13)
    ax.set_xticks([0,.5,1])
    ax.xaxis.set_major_formatter(PercentFormatter(1))
    ax.set_title(title,fontsize=10,pad=12)
    ax.set_xlabel("Mean message recall@8")
    ax.set_axisbelow(True)
    ax.grid(axis="x",alpha=.16)
save(fig,"category_recall")

fig,ax = plt.subplots(figsize=(7.15,3.3),layout="constrained")
for kind,label,color in [("selective","Unique marker lookup","#275c85"),("broad","Broad matching query","#676f79")]:
    group = [r for r in operations["scaling"] if r["query_kind"]==kind]
    ax.plot([r["messages"] for r in group],[r["median_ms"] for r in group],marker="o",lw=1.6,color=color,label=label)
    for row in group:
        ax.annotate(f"{row['median_ms']:.3f}",(row["messages"],row["median_ms"]),xytext=(0,9),textcoords="offset points",ha="center",fontsize=8)
ax.set_xscale("log")
ax.set_yscale("log")
ax.set_xticks([1000,10000,50000],["1,000","10,000","50,000"])
ax.set_ylim(.025,170)
ax.set_xlim(700,75000)
ax.set_xlabel("Stored messages (log scale)")
ax.set_ylabel("Median query latency, ms (log scale)")
ax.legend(frameon=False,loc="upper left")
ax.grid(alpha=.17,which="major")
save(fig,"search_scale")

(root/"results/chart_data.json").write_text(json.dumps({"retrieval":[r for r in public["groups"] if r["category"]=="all"],"categories":[r for r in public["groups"] if r["method"]=="bm25" and r["category"]!="all"],"scaling":operations["scaling"]},indent=2)+"\n")
print("Rendered three figures from measured data")
