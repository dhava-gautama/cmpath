"""Render PAPER.md, measured figures and numbered source notes to a PDF."""
from pathlib import Path
import json
import re
from xml.sax.saxutils import escape,quoteattr
from matplotlib import get_data_path
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                               TableStyle, Image, KeepTogether, Preformatted)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT/"output/pdf/ContextMemoryPath_Research.pdf"
SOURCES = {s["id"]:s for s in json.loads((ROOT/"research/sources.json").read_text())}
FONTS = Path(get_data_path())/"fonts/ttf"
for name,file in [("Body","DejaVuSerif.ttf"),("BodyBold","DejaVuSerif-Bold.ttf"),
                  ("BodyItalic","DejaVuSerif-Italic.ttf"),("Sans","DejaVuSans.ttf"),
                  ("SansBold","DejaVuSans-Bold.ttf"),("Mono","DejaVuSansMono.ttf")]:
    pdfmetrics.registerFont(TTFont(name,str(FONTS/file)))
pdfmetrics.registerFontFamily("Body",normal="Body",bold="BodyBold",italic="BodyItalic",boldItalic="BodyBold")
pdfmetrics.registerFontFamily("Sans",normal="Sans",bold="SansBold",italic="Sans",boldItalic="SansBold")

styles = {
 "body":ParagraphStyle("body",fontName="Body",fontSize=9.8,leading=14,spaceAfter=8,allowWidows=0,allowOrphans=0,textColor=colors.HexColor("#242424")),
 "title":ParagraphStyle("title",fontName="SansBold",fontSize=20,leading=25,spaceAfter=15),
 "h2":ParagraphStyle("h2",fontName="SansBold",fontSize=13.1,leading=17,spaceBefore=13,spaceAfter=8,keepWithNext=True),
 "h3":ParagraphStyle("h3",fontName="SansBold",fontSize=10.5,leading=14,spaceBefore=9,spaceAfter=7,keepWithNext=True),
 "cell":ParagraphStyle("cell",fontName="Sans",fontSize=8.2,leading=11),
 "head":ParagraphStyle("head",fontName="SansBold",fontSize=8.2,leading=11,textColor=colors.white),
 "caption":ParagraphStyle("caption",fontName="Sans",fontSize=8.2,leading=11.5,spaceAfter=10,textColor=colors.HexColor("#454545")),
 "code":ParagraphStyle("code",fontName="Mono",fontSize=7.4,leading=10.5,spaceAfter=10),
 "source":ParagraphStyle("source",fontName="Sans",fontSize=8.6,leading=12,spaceAfter=7,allowWidows=0,allowOrphans=0),
}


def inline(text):
    pattern = r"\[\^(\d+)\]|\[([^\]]+)\]\(([^)]+)\)|`([^`]+)`|\*\*([^*]+)\*\*|\*([^*]+)\*"
    out,cursor = [],0
    for match in re.finditer(pattern,text):
        out.append(escape(text[cursor:match.start()]))
        if match[1]:
            ref = SOURCES[int(match[1])]
            if cursor == match.start() and out and out[-1] == "" and len(out) > 1 and out[-2].endswith("</super>"):
                out.append("<super>,</super>")
            out.append("<super><link href="+quoteattr(ref["url"])+">"+match[1]+"</link></super>")
        elif match[2]:
            if match[3].startswith(("https://","http://")):
                out.append("<link color='#275c85' href="+quoteattr(match[3])+">"+escape(match[2])+"</link>")
            else:
                out.append(escape(match[2]))
        elif match[4]:
            out.append("<font name='Mono' size='8'>"+escape(match[4])+"</font>")
        elif match[5]:
            out.append("<b>"+escape(match[5])+"</b>")
        else:
            out.append("<i>"+escape(match[6])+"</i>")
        cursor = match.end()
    out.append(escape(text[cursor:]))
    return "".join(out)


class CitedParagraph(Paragraph):
    def split(self,*args,**kwargs):
        result = super().split(*args,**kwargs)
        def visible_sources(fragment):
            if isinstance(fragment,(list,tuple)):
                found = set()
                for part in fragment:
                    found.update(visible_sources(part))
                return found
            text = getattr(fragment,"text","")
            if getattr(fragment,"rise",0)>0 and getattr(fragment,"link",[]) and text.isdigit():
                return {int(text)}
            return set()
        for item in result:
            item.source_ids = visible_sources(item.frags) & getattr(self,"source_ids",set())
        return result


def para(text,style="body"):
    p = CitedParagraph(inline(text),styles[style])
    p.source_ids = {int(n) for n in re.findall(r"\[\^(\d+)\]",text)}
    return p


class Report(SimpleDocTemplate):
    def beforePage(self):
        self.page_sources = set()

    def afterFlowable(self,flowable):
        self.page_sources.update(getattr(flowable,"source_ids",set()))

    def afterPage(self):
        canvas = self.canv
        canvas.saveState()
        columns = 2 if len(self.page_sources)>7 else 1
        per_column = (len(self.page_sources)+columns-1)//columns
        canvas.setFont("Sans",7.1)
        canvas.setFillColor(colors.HexColor("#474747"))
        for index,number in enumerate(sorted(self.page_sources)):
            source = SOURCES[number]
            x = 48+(index//per_column)*255
            y = 84-(index%per_column)*9
            label = f"{number}. {source['short']}"
            if pdfmetrics.stringWidth(label,"Sans",7.1) > (245 if columns==2 else 499):
                raise ValueError("Source note exceeds the available width")
            canvas.drawString(x,y,label)
            canvas.linkURL(source["url"],(x,y-2,x+(245 if columns==2 else 499),y+7),relative=0)
        canvas.setFont("Sans",7.5)
        canvas.drawRightString(547,23,str(self.page))
        canvas.restoreState()


def build():
    lines = (ROOT/"PAPER.md").read_text().splitlines()
    story,i,in_sources = [],0,False
    while i<len(lines):
        line = lines[i].strip()
        if not line or re.match(r"\[\^\d+\]:",line):
            i += 1
            continue
        if line.startswith("```"):
            i += 1
            code = []
            while i<len(lines) and not lines[i].startswith("```"):
                code.append(lines[i]); i += 1
            story.append(Preformatted("\n".join(code),styles["code"]))
            i += 1
            continue
        if line.startswith("|"):
            rows = []
            while i<len(lines) and lines[i].strip().startswith("|"):
                row = [cell.strip() for cell in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r"[-: ]+",cell) for cell in row):
                    rows.append(row)
                i += 1
            n = len(rows[0])
            ratios = {2:[.43,.57],3:[.25,.35,.40],4:[.34,.22,.22,.22],5:[.34,.165,.165,.165,.165]}.get(n,[1/n]*n)
            table = Table([[para(cell,"head" if ri==0 else "cell") for cell in row] for ri,row in enumerate(rows)],colWidths=[499*r for r in ratios],repeatRows=1,hAlign="LEFT")
            table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#343a40")),
                                      ("VALIGN",(0,0),(-1,-1),"TOP"),
                                      ("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),
                                      ("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
                                      ("ROWBACKGROUNDS",(0,1),(-1,-1),[colors.HexColor("#f2f3f4"),colors.white]),
                                      ("LINEBELOW",(0,1),(-1,-1),.25,colors.HexColor("#dedede"))]))
            if table.wrap(499,690)[1]<260:
                story.append(KeepTogether([table,Spacer(1,10)]))
            else:
                story.extend([table,Spacer(1,10)])
            continue
        match = re.fullmatch(r"!\[([^\]]*)\]\(([^)]+)\)",line)
        if match:
            path = ROOT/match[2]
            width,height = ImageReader(str(path)).getSize()
            picture = Image(str(path),width=499,height=499*height/width)
            i += 1
            while i<len(lines) and not lines[i].strip():
                i += 1
            content = [picture,Spacer(1,5)]
            if i<len(lines) and lines[i].startswith("**Figure"):
                content.append(para(lines[i],"caption")); i += 1
            story.append(KeepTogether(content))
            continue
        for prefix,style in [("### ","h3"),("## ","h2"),("# ","title")]:
            if line.startswith(prefix):
                story.append(para(line[len(prefix):],style))
                if line=="## Sources":
                    in_sources = True
                i += 1
                break
        else:
            paragraph = [line]
            i += 1
            while i<len(lines) and lines[i].strip() and not lines[i].startswith(("#","|","```","![","[^")):
                paragraph.append(lines[i].strip()); i += 1
            story.append(para(" ".join(paragraph),"source" if in_sources else "body"))
    OUTPUT.parent.mkdir(parents=True,exist_ok=True)
    doc = Report(str(OUTPUT),pagesize=(595,842),leftMargin=48,rightMargin=48,
                 topMargin=43,bottomMargin=104,
                 title="Context Memory Path: Durable Task State and Evidence Retrieval",
                 author="CMP contributors",subject="Software artifact and reproducible retrieval evaluation")
    doc.build(story)
    print(OUTPUT)


if __name__ == "__main__":
    build()
