"""15秒商品短视频：事实约束分镜、并行本地运镜、逐镜头版本和最终合成。"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
from sqlalchemy import func, select

from .database import GeneratedMedia, VideoPlan, session_scope, utcnow
from .media_pipeline import _font, _platform_record, _sha, _tts_audio, _video_lines

VERSION="storyboard-video-v1.0"
SHOT_COUNT=5
SHOT_SECONDS=3
SIZES={"9:16":(1080,1920),"1:1":(1080,1080),"16:9":(1920,1080)}
ROLES=["痛点引入","商品整体","使用场景","细节特写","规格与行动引导"]
MOTIONS=["slow_zoom_in","pan_left","slow_zoom_out","pan_right","slow_zoom_in"]


def _dump(value):return json.dumps(value,ensure_ascii=False,sort_keys=True)


def _ffmpeg()->str:
    configured=os.getenv("FFMPEG_PATH","").strip()
    if configured:return configured
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:return "ffmpeg"


def _normalize_mode(value)->str:
    mode=str(value or "conservative").strip()
    if mode not in {"conservative","creative"}:raise ValueError("短视频模式只支持保守改造或创意重构")
    return mode


def _normalize_ratio(value,platform)->str:
    ratio=str(value or "auto").strip()
    if ratio=="auto":ratio="9:16" if platform=="抖音" else "1:1"
    if ratio not in SIZES:raise ValueError("视频比例只支持9:16、1:1或16:9")
    return ratio


def _source_images(root:Path,content_id:str,product_id:str,content_version:int,mode:str)->list[dict]:
    with session_scope(root) as s:
        rows=list(s.scalars(select(GeneratedMedia).where(
            GeneratedMedia.product_id==product_id,GeneratedMedia.content_id==content_id,
            GeneratedMedia.media_type=="image",GeneratedMedia.status=="success",
            GeneratedMedia.local_path.is_not(None)).order_by(GeneratedMedia.created_at.desc())))
    output=[];seen=set()
    for row in rows:
        metadata=json.loads(row.metadata_json or "{}")
        if int(metadata.get("content_version") or 0)!=int(content_version):continue
        actual=metadata.get("generation_mode")
        if actual not in {mode,"text_only_concept","text-to-image-concept"}:continue
        if row.slot in seen:continue
        path=(root/row.local_path).resolve()
        try:path.relative_to((root/"data/media").resolve())
        except ValueError:continue
        if not path.is_file():continue
        seen.add(row.slot);output.append({"media_id":row.media_id,"slot":row.slot,"path":path,
            "generation_mode":actual,"sha256":row.sha256})
        if len(output)>=5:break
    if len(output)<3:raise ValueError(f"请先生成至少3张{('创意重构' if mode=='creative' else '保守改造')}图片")
    return output


def _ai_layout(platform:str,title:str,sources:list[dict])->tuple[list[dict]|None,dict]:
    """模型只决定镜头职责、素材和运镜；字幕由已审核字段提供。"""
    try:
        from langchain_openai import ChatOpenAI
        from .agents import _json
        from .model_config import load_model_config
        config=load_model_config("trending")
        prompt=("你是电商15秒短视频分镜师。只从给定media_id选择每个镜头的关键帧，不改写商品事实。"
            "返回JSON：{\"shots\":[{\"index\":1,\"role\":\"\",\"source_media_id\":\"\","
            "\"motion\":\"slow_zoom_in|slow_zoom_out|pan_left|pan_right\",\"visual_brief\":\"\"}]}。"
            "必须恰好5镜头，index为1到5，分别覆盖痛点、商品、场景、细节、规格/收束。"
            f"平台={platform}；商品={title}；允许素材="+
            _dump([{"media_id":x["media_id"],"slot":x["slot"]} for x in sources]))
        model=ChatOpenAI(api_key=config.api_key,base_url=config.base_url,model=config.model,
            temperature=0,timeout=90,max_retries=0,extra_body={"enable_thinking":False})
        raw=_json(model.invoke(prompt).content);shots=raw.get("shots") if isinstance(raw,dict) else None
        allowed={x["media_id"] for x in sources}
        if not isinstance(shots,list) or len(shots)!=SHOT_COUNT:return None,{"source":"rules-fallback","error_type":"invalid_count"}
        clean=[]
        for index,item in enumerate(shots,1):
            media_id=str(item.get("source_media_id") or "")
            if media_id not in allowed:raise ValueError("模型引用了不存在的素材")
            motion=str(item.get("motion") or MOTIONS[index-1])
            if motion not in set(MOTIONS):motion=MOTIONS[index-1]
            clean.append({"index":index,"role":ROLES[index-1],"source_media_id":media_id,
                "motion":motion,"visual_brief":str(item.get("visual_brief") or "")[:300]})
        return clean,{"source":"ai","model":config.model}
    except Exception as exc:return None,{"source":"rules-fallback","error_type":type(exc).__name__}


def plan_video(root:Path,content_id:str,generation_mode:str="conservative",aspect_ratio:str="auto",force:bool=False)->dict:
    root=root.resolve();content,master,fields,content_version=_platform_record(root,content_id)
    mode=_normalize_mode(generation_mode);ratio=_normalize_ratio(aspect_ratio,content.platform)
    sources=_source_images(root,content_id,master.product_id,content_version,mode)
    lines=_video_lines(fields)
    captions=(lines+["查看商品规格","商品信息以页面参数为准"]*3)[:SHOT_COUNT]
    fingerprint=hashlib.sha256(_dump({"content_id":content_id,"version":content_version,"mode":mode,
        "ratio":ratio,"sources":[x["sha256"] for x in sources],"captions":captions,"pipeline":VERSION}).encode()).hexdigest()
    if not force:
        with session_scope(root) as s:
            old=s.scalar(select(VideoPlan).where(VideoPlan.content_id==content_id,VideoPlan.input_hash==fingerprint)
                .order_by(VideoPlan.created_at.desc()))
            if old:return video_plan(root,old.plan_id)
    layout,planner=_ai_layout(content.platform,fields.get("商品标题") or fields.get("标题") or "商品",sources)
    if not layout:
        layout=[{"index":i,"role":ROLES[i-1],"source_media_id":sources[(i-1)%len(sources)]["media_id"],
            "motion":MOTIONS[i-1],"visual_brief":f"使用{sources[(i-1)%len(sources)]['slot']}呈现{ROLES[i-1]}"}
            for i in range(1,SHOT_COUNT+1)]
    source_map={x["media_id"]:x for x in sources}
    shots=[]
    for item,caption in zip(layout,captions):
        source=source_map[item["source_media_id"]]
        shots.append({**item,"duration_seconds":SHOT_SECONDS,"caption":caption,"source_slot":source["slot"],
            "source_sha256":source["sha256"],"status":"planned"})
    plan_id="VP-"+uuid.uuid4().hex[:16];stamp=utcnow()
    payload={"plan_id":plan_id,"content_id":content_id,"product_id":master.product_id,"platform":content.platform,
        "content_version":content_version,"generation_mode":mode,"aspect_ratio":ratio,"size":list(SIZES[ratio]),
        "duration_seconds":15,"shots":shots,"planner":planner,"pipeline_version":VERSION}
    with session_scope(root) as s:s.add(VideoPlan(plan_id=plan_id,content_id=content_id,input_hash=fingerprint,
        status="planned",payload=_dump(payload),created_at=stamp,updated_at=stamp))
    return video_plan(root,plan_id)


def _frame(source:Path,target:Path,size:tuple[int,int],caption:str,role:str)->None:
    image=Image.open(source).convert("RGB");background=image.copy();background.thumbnail(size,Image.Resampling.LANCZOS)
    canvas=Image.new("RGB",size,(235,241,238));blur=image.resize(size,Image.Resampling.LANCZOS).filter(ImageFilter.GaussianBlur(35))
    canvas.paste(blur,(0,0));scale=min(size[0]/image.width,size[1]/image.height)*.91
    product=image.resize((max(1,int(image.width*scale)),max(1,int(image.height*scale))),Image.Resampling.LANCZOS)
    canvas.paste(product,((size[0]-product.width)//2,(size[1]-product.height)//2))
    draw=ImageDraw.Draw(canvas,"RGBA");margin=max(28,size[0]//24);bar_h=max(170,size[1]//7)
    draw.rounded_rectangle((margin,size[1]-bar_h-margin,size[0]-margin,size[1]-margin),radius=24,fill=(10,24,20,185))
    role_font=_font(max(24,size[0]//40));caption_font=_font(max(30,size[0]//31))
    draw.text((margin+26,size[1]-bar_h-margin+16),role,font=role_font,fill=(191,231,213,255))
    text=re.sub(r"\s+"," ",caption).strip()[:32]
    available=size[0]-2*margin-52;lines=[];current=""
    for char in text:
        candidate=current+char
        if current and draw.textbbox((0,0),candidate,font=caption_font)[2]>available:
            lines.append(current);current=char
        else:current=candidate
    if current:lines.append(current)
    lines=lines[:2]
    if len(lines)==2 and ''.join(lines)!=text:lines[1]=lines[1][:-1]+'…'
    draw.multiline_text((margin+26,size[1]-bar_h-margin+56),'\n'.join(lines),font=caption_font,
        fill="white",spacing=7)
    canvas.save(target,"PNG")


def _clip(root:Path,plan:dict,shot:dict,source:Path,version:int)->dict:
    folder=root/"data/media"/plan["product_id"]/"generated"/plan["content_id"]/"video"/plan["plan_id"]
    folder.mkdir(parents=True,exist_ok=True);size=tuple(plan["size"]);index=shot["index"]
    frame=folder/f"shot-{index}-frame-v{version}.png";path=folder/f"shot-{index}-v{version}.mp4"
    _frame(source,frame,size,shot["caption"],shot["role"])
    direction={"pan_left":"x='max(0,iw-iw/zoom-on*1.5)'","pan_right":"x='min(iw-iw/zoom,on*1.5)'",
        "slow_zoom_out":"z='if(eq(on,1),1.08,max(1.0,zoom-0.0009))'"}.get(shot["motion"],"z='min(zoom+0.0009,1.08)'")
    zoom=direction if direction.startswith("z=") else "z='min(zoom+0.0007,1.05)':"+direction
    vf=f"zoompan={zoom}:y='ih/2-(ih/zoom/2)':d=90:s={size[0]}x{size[1]}:fps=30,format=yuv420p"
    command=[_ffmpeg(),"-y","-loop","1","-i",str(frame),"-t",str(SHOT_SECONDS),"-vf",vf,
        "-an","-c:v","libx264","-preset","medium","-movflags","+faststart",str(path)]
    subprocess.run(command,check=True,capture_output=True,timeout=120)
    return {"path":path,"frame":frame,"sha256":_sha(path.read_bytes())}


def _latest_clips(root:Path,plan:dict)->dict[int,GeneratedMedia]:
    with session_scope(root) as s:
        rows=list(s.scalars(select(GeneratedMedia).where(GeneratedMedia.content_id==plan["content_id"],
            GeneratedMedia.media_type=="video_clip").order_by(GeneratedMedia.version.desc(),GeneratedMedia.created_at.desc())))
        output={}
        for row in rows:
            meta=json.loads(row.metadata_json or "{}")
            if meta.get("plan_id")!=plan["plan_id"]:continue
            index=int(meta.get("shot_index") or 0)
            if index and index not in output:output[index]=row
        return output


def _assemble(root:Path,plan:dict,clips:dict[int,GeneratedMedia])->GeneratedMedia:
    folder=root/"data/media"/plan["product_id"]/"generated"/plan["content_id"]/"video"/plan["plan_id"]
    manifest=folder/"clips.txt";paths=[(root/clips[i].local_path).resolve() for i in range(1,SHOT_COUNT+1)]
    manifest.write_text("".join(f"file '{path.as_posix()}'\n" for path in paths),encoding="utf-8")
    with session_scope(root) as s:
        highest=s.scalar(select(func.max(GeneratedMedia.version)).where(GeneratedMedia.product_id==plan["product_id"],
            GeneratedMedia.content_id==plan["content_id"],GeneratedMedia.media_type=="video"));version=(highest or 0)+1
    silent=folder/f"video-silent-v{version}.mp4";path=folder/f"video-v{version}.mp4"
    subprocess.run([_ffmpeg(),"-y","-f","concat","-safe","0","-i",str(manifest),"-t","15","-c","copy",str(silent)],
        check=True,capture_output=True,timeout=120)
    audio,audio_status=_tts_audio("。".join(x["caption"] for x in plan["shots"]))
    if audio:
        audio_path=folder/f"voice-v{version}.mp3";audio_path.write_bytes(audio)
        subprocess.run([_ffmpeg(),"-y","-i",str(silent),"-i",str(audio_path),"-t","15","-c:v","copy",
            "-c:a","aac","-af","apad=pad_dur=15","-movflags","+faststart",str(path)],check=True,capture_output=True,timeout=120)
    else:silent.replace(path)
    media_id="MD-"+uuid.uuid4().hex[:16]
    metadata={"plan_id":plan["plan_id"],"duration_seconds":15,"content_version":plan["content_version"],
        "pipeline_version":VERSION,"generation_mode":plan["generation_mode"],"aspect_ratio":plan["aspect_ratio"],
        "size":plan["size"],"clip_media_ids":[clips[i].media_id for i in range(1,SHOT_COUNT+1)],
        "audio_status":audio_status,"engine":"local_motion_mvp","review":{"passed":True,"checks":["5个镜头","总时长15秒","H.264"]}}
    item=GeneratedMedia(media_id=media_id,product_id=plan["product_id"],content_id=plan["content_id"],media_type="video",
        slot="15秒短视频",version=version,status="success",local_path=str(path.relative_to(root)),sha256=_sha(path.read_bytes()),
        metadata_json=_dump(metadata),created_at=utcnow())
    with session_scope(root) as s:s.add(item)
    return item


def generate_video(root:Path,content_id:str,plan_id:str|None=None,shot_indexes:list[int]|None=None,
        progress_callback=None)->dict:
    root=root.resolve()
    if plan_id:
        with session_scope(root) as s:row=s.get(VideoPlan,plan_id)
        if not row or row.content_id!=content_id:raise ValueError("视频分镜不存在")
        plan=json.loads(row.payload)
    else:plan=plan_video(root,content_id)
    sources={x.media_id:(root/x.local_path).resolve() for x in _media_rows(root,plan)}
    wanted=sorted(set(shot_indexes or range(1,SHOT_COUNT+1)))
    if any(x<1 or x>SHOT_COUNT for x in wanted):raise ValueError("镜头编号必须在1到5之间")
    completed=0;success=0;failed=0;lock=threading.Lock();items=[]
    def worker(shot):
        nonlocal completed,success,failed
        slot=f"分镜-{shot['index']}";source=sources.get(shot["source_media_id"])
        if not source or not source.is_file():raise ValueError(f"{slot}来源图片不存在")
        with session_scope(root) as s:
            highest=s.scalar(select(func.max(GeneratedMedia.version)).where(GeneratedMedia.content_id==content_id,
                GeneratedMedia.media_type=="video_clip",GeneratedMedia.slot==slot));version=(highest or 0)+1
        media_id="VC-"+uuid.uuid4().hex[:16]
        try:
            result=_clip(root,plan,shot,source,version);status="success";error=None
        except Exception as exc:result={"path":None,"sha256":None};status="failed";error=type(exc).__name__
        meta={"plan_id":plan["plan_id"],"shot_index":shot["index"],"duration_seconds":SHOT_SECONDS,
            "source_media_id":shot["source_media_id"],"caption":shot["caption"],"role":shot["role"],
            "motion":shot["motion"],"generation_mode":plan["generation_mode"],"engine":"local_motion_mvp",
            "review":{"passed":status=="success","error_type":error}}
        with session_scope(root) as s:s.add(GeneratedMedia(media_id=media_id,product_id=plan["product_id"],content_id=content_id,
            media_type="video_clip",slot=slot,version=version,status=status,
            local_path=str(result["path"].relative_to(root)) if result["path"] else None,sha256=result["sha256"],
            metadata_json=_dump(meta),created_at=utcnow()))
        with lock:
            completed+=1;success+=status=="success";failed+=status!="success"
            if progress_callback:progress_callback(completed,len(wanted),2,success,failed)
        return {"media_id":media_id,"shot_index":shot["index"],"status":status,"error_type":error}
    shots=[x for x in plan["shots"] if x["index"] in wanted]
    with ThreadPoolExecutor(max_workers=min(2,len(shots))) as pool:
        futures=[pool.submit(worker,shot) for shot in shots]
        for future in as_completed(futures):items.append(future.result())
    clips=_latest_clips(root,plan);usable={i:x for i,x in clips.items() if x.status=="success" and x.local_path}
    final=None
    if len(usable)==SHOT_COUNT:final=_assemble(root,plan,usable)
    with session_scope(root) as s:
        row=s.get(VideoPlan,plan["plan_id"]);row.status="completed" if final else "partial";row.updated_at=utcnow()
    return {"plan_id":plan["plan_id"],"content_id":content_id,"items":sorted(items,key=lambda x:x["shot_index"]),
        "completed_slots":completed,"total_slots":len(wanted),"success_count":success,"failed_count":failed,
        "media_id":final.media_id if final else None,"status":"completed" if final else "partial"}


def _media_rows(root:Path,plan:dict)->list[GeneratedMedia]:
    ids={x["source_media_id"] for x in plan["shots"]}
    with session_scope(root) as s:return list(s.scalars(select(GeneratedMedia).where(GeneratedMedia.media_id.in_(ids))))


def video_plan(root:Path,plan_id:str)->dict:
    with session_scope(root) as s:
        row=s.get(VideoPlan,plan_id)
        if not row:raise ValueError("视频分镜不存在")
        payload=json.loads(row.payload);status=row.status
    clips=_latest_clips(root,payload);view=[]
    for shot in payload["shots"]:
        clip=clips.get(shot["index"]);view.append({**shot,"status":clip.status if clip else shot["status"],
            "clip_media_id":clip.media_id if clip else None,"clip_url":f"/api/media/files/{clip.media_id}" if clip and clip.local_path else None,
            "review":json.loads(clip.metadata_json).get("review") if clip else None})
    return {**payload,"status":status,"shots":view}


def latest_video_plan(root:Path,content_id:str)->dict|None:
    with session_scope(root) as s:row=s.scalar(select(VideoPlan).where(VideoPlan.content_id==content_id).order_by(VideoPlan.created_at.desc()))
    return video_plan(root,row.plan_id) if row else None
