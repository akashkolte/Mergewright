"""Inject slide transitions and entrance build animations into a PptxGenJS deck.

Shapes are tagged by objectName: A<step>_<effect>_<delayMs>_<anything>.
step 0 plays automatically after the slide appears; steps 1..n are clicks.
Within a step, effects play together, offset by their delay.
"""
import re
import sys
import zipfile

EFFECTS = {
    "fade": (10, 0, "fade"),
    "wipeL": (22, 8, "wipe(left)"),
    "wipeR": (22, 2, "wipe(right)"),
    "wipeT": (22, 1, "wipe(up)"),
    "wipeB": (22, 4, "wipe(down)"),
}
DUR = 500
TAG = re.compile(r'<p:nv(Sp|Pic|CxnSp)Pr>\s*<p:cNvPr id="(\d+)" name="A(\d+)_([A-Za-z]+)_(\d+)_[^"]*"')
TRANSITION = '<p:transition spd="med"><p:fade/></p:transition>'


def effect_xml(ids, shape, node):
    preset, subtype, flt = EFFECTS[shape["eff"]]
    c1, c2, c3 = next(ids), next(ids), next(ids)
    sp = shape["spid"]
    return (
        f'<p:par><p:cTn id="{c1}" presetID="{preset}" presetClass="entr" presetSubtype="{subtype}" '
        f'fill="hold" grpId="0" nodeType="{node}"><p:stCondLst><p:cond delay="{shape["delay"]}"/></p:stCondLst>'
        f'<p:childTnLst><p:set><p:cBhvr><p:cTn id="{c2}" dur="1" fill="hold"><p:stCondLst><p:cond delay="0"/>'
        f'</p:stCondLst></p:cTn><p:tgtEl><p:spTgt spid="{sp}"/></p:tgtEl><p:attrNameLst>'
        f'<p:attrName>style.visibility</p:attrName></p:attrNameLst></p:cBhvr><p:to><p:strVal val="visible"/>'
        f'</p:to></p:set><p:animEffect transition="in" filter="{flt}"><p:cBhvr><p:cTn id="{c3}" dur="{DUR}"/>'
        f'<p:tgtEl><p:spTgt spid="{sp}"/></p:tgtEl></p:cBhvr></p:animEffect></p:childTnLst></p:cTn></p:par>'
    )


def timing_xml(shapes):
    counter = iter(range(3, 100000))
    pars = []
    for step in sorted({s["step"] for s in shapes}):
        items = sorted((s for s in shapes if s["step"] == step), key=lambda s: (s["delay"], s["order"]))
        outer, inner = next(counter), next(counter)
        cond = '<p:cond delay="indefinite"/>'
        if step == 0:
            cond += '<p:cond evt="onBegin" delay="0"><p:tn val="2"/></p:cond>'
        effs = []
        for k, s in enumerate(items):
            node = ("afterEffect" if step == 0 else "clickEffect") if k == 0 else "withEffect"
            effs.append(effect_xml(counter, s, node))
        pars.append(
            f'<p:par><p:cTn id="{outer}" fill="hold"><p:stCondLst>{cond}</p:stCondLst><p:childTnLst>'
            f'<p:par><p:cTn id="{inner}" fill="hold"><p:stCondLst><p:cond delay="0"/></p:stCondLst>'
            f'<p:childTnLst>{"".join(effs)}</p:childTnLst></p:cTn></p:par></p:childTnLst></p:cTn></p:par>'
        )
    bld = "".join(f'<p:bldP spid="{s["spid"]}" grpId="0" animBg="1"/>' for s in shapes if s["kind"] == "Sp")
    return (
        '<p:timing><p:tnLst><p:par><p:cTn id="1" dur="indefinite" restart="never" nodeType="tmRoot">'
        '<p:childTnLst><p:seq concurrent="1" nextAc="seek"><p:cTn id="2" dur="indefinite" nodeType="mainSeq">'
        f'<p:childTnLst>{"".join(pars)}</p:childTnLst></p:cTn>'
        '<p:prevCondLst><p:cond evt="onPrev" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:prevCondLst>'
        '<p:nextCondLst><p:cond evt="onNext" delay="0"><p:tgtEl><p:sldTgt/></p:tgtEl></p:cond></p:nextCondLst>'
        '</p:seq></p:childTnLst></p:cTn></p:par></p:tnLst>'
        + (f"<p:bldLst>{bld}</p:bldLst>" if bld else "")
        + "</p:timing>"
    )


def process(xml):
    shapes = []
    seen = set()
    for i, m in enumerate(TAG.finditer(xml)):
        kind, spid, step, eff, delay = m.groups()
        if eff not in EFFECTS:
            raise ValueError(f"unknown effect {eff}")
        if spid in seen:
            raise ValueError(f"duplicate shape id {spid}")
        seen.add(spid)
        shapes.append(dict(kind=kind, spid=spid, step=int(step), eff=eff, delay=int(delay), order=i))
    extra = TRANSITION + (timing_xml(shapes) if shapes else "")
    if "</p:clrMapOvr>" not in xml:
        raise ValueError("no clrMapOvr to anchor timing")
    return xml.replace("</p:clrMapOvr>", "</p:clrMapOvr>" + extra, 1), len(shapes)


def main(src, dst):
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if re.fullmatch(r"ppt/slides/slide\d+\.xml", item.filename):
                xml, n = process(data.decode("utf-8"))
                data = xml.encode("utf-8")
                print(f"{item.filename}: {n} animated shapes")
            zout.writestr(item, data)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
