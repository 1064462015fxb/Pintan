"""媒体闭环的离线安全与授权门禁测试，不调用付费模型。"""
from __future__ import annotations

import socket
import subprocess
import tempfile
import unittest
import io
from pathlib import Path
from unittest.mock import patch
from PIL import Image

from pintan.database import Base, Product, ProductAsset, AssetAuthorization, engine, session_scope
from pintan.media_pipeline import (_background_quality, _extract_images, _resolve_file,
                                   _safe_public_url, _trim_subject, _video_lines, authorize_asset, upload_asset)
from pintan.asset_records import metadata


class MediaSecurityTests(unittest.TestCase):
    def test_extract_deduplicates_images(self):
        html='<meta property="og:image" content="//img.example/a.jpg"><img src="//img.example/a.jpg"><img data-src="/b.png">'
        self.assertEqual(_extract_images(html,"https://detail.1688.com/offer/1.html"),[
            "https://img.example/a.jpg","https://detail.1688.com/b.png"])

    @patch("pintan.media_pipeline.socket.getaddrinfo")
    def test_ssrf_rejects_private_ip(self, lookup):
        lookup.return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,"",("127.0.0.1",443))]
        with self.assertRaisesRegex(ValueError,"内网"):_safe_public_url("https://example.com/a.jpg")

    def test_media_path_cannot_escape_directory(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/"secret.txt").write_text("x",encoding="utf-8")
            with self.assertRaisesRegex(ValueError,"路径无效"):_resolve_file(root,"secret.txt")

    def test_video_text_comes_from_existing_fields(self):
        lines=_video_lines({"口播":["第一段","第二段"],"核心卖点":["第三段"]})
        self.assertEqual(lines,["第一段","第二段","第三段"])

    def test_transparent_subject_is_trimmed(self):
        import io
        from PIL import Image
        source=Image.new("RGBA",(100,100),(0,0,0,0))
        for x in range(20,60):
            for y in range(30,80):source.putpixel((x,y),(255,0,0,255))
        raw=io.BytesIO();source.save(raw,"PNG")
        self.assertEqual(Image.open(io.BytesIO(_trim_subject(raw.getvalue()))).size,(40,50))

    def test_background_complexity_rejects_dense_pattern(self):
        import cv2,numpy as np
        image=np.zeros((400,400,3),np.uint8)
        for x in range(0,400,8):cv2.line(image,(x,0),(x,399),(255,255,255),2)
        ok,_=_background_quality(cv2.imencode('.png',image)[1].tobytes(),"主图")
        self.assertFalse(ok)

    def test_bundled_ffmpeg_builds_fifteen_second_h264(self):
        from PIL import Image
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as d:
            folder=Path(d);images=[]
            for index,color in enumerate(("red","green","blue")):
                path=folder/f"{index}.png";Image.new("RGB",(320,320),color).save(path);images.append(path)
            manifest=folder/"slides.txt"
            manifest.write_text("".join(f"file '{p.as_posix()}'\nduration 5\n" for p in images)+f"file '{images[-1].as_posix()}'\n",encoding="utf-8")
            output=folder/"test.mp4"
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(),"-y","-f","concat","-safe","0","-i",str(manifest),
                "-t","15","-vf","fps=30,format=yuv420p","-c:v","libx264",str(output)],check=True,capture_output=True)
            _,seconds=imageio_ffmpeg.count_frames_and_secs(str(output))
            self.assertLessEqual(abs(seconds-15),.2)


class AuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        Base.metadata.create_all(engine(self.root))
        source=self.root/"data/media/P1/source";source.mkdir(parents=True)
        (source/"a.png").write_bytes(b"not-an-image-but-hashed")
        with session_scope(self.root) as s:
            s.add(Product(product_id="P1",title="测试",category="日用品"))
            s.add(ProductAsset(asset_id="A1",product_id="P1",asset_type="原图",status="待授权确认",
                local_path="data/media/P1/source/a.png",sha256="abc",authorization_status="未确认"))

    def tearDown(self):self.tmp.cleanup()

    def test_exact_human_confirmation_is_required(self):
        with self.assertRaises(ValueError):authorize_asset(self.root,"A1",False,"")
        with self.assertRaises(ValueError):authorize_asset(self.root,"A1",True,"我同意")
        result=authorize_asset(self.root,"A1",True,"我已确认拥有该图片的使用权")
        self.assertTrue(result["authorized"])
        with session_scope(self.root) as s:
            auth=s.query(AssetAuthorization).one()
            self.assertEqual(auth.asset_sha256,"abc")

    def test_manual_parameter_image_does_not_require_subject_cutout(self):
        raw=io.BytesIO();Image.new("RGB",(400,400),"white").save(raw,format="PNG")
        with patch("pintan.media_pipeline._cutout",side_effect=ValueError("no product")),patch("pintan.media_pipeline._ocr",return_value="待复核"):
            result=upload_asset(self.root,"P1",raw.getvalue(),"image/png","参数图")
        self.assertEqual(result["asset_type"],"参数图")
        self.assertEqual(result["cutout_status"],"不适用或未识别主体")
        with session_scope(self.root) as s:
            asset=s.get(ProductAsset,result["asset_id"])
            self.assertEqual(asset.asset_type,"参数图");self.assertEqual(asset.authorization_status,"未确认")
            self.assertEqual(metadata(s,asset.asset_id)["source_kind"],"人工上传")

    def test_manual_asset_type_is_limited(self):
        raw=io.BytesIO();Image.new("RGB",(400,400),"white").save(raw,format="PNG")
        with self.assertRaisesRegex(ValueError,"素材类型"):
            upload_asset(self.root,"P1",raw.getvalue(),"image/png","无效类型")

    def test_uploaded_information_image_runs_ocr_and_keeps_review_status(self):
        raw=io.BytesIO();Image.new("RGB",(500,500),"white").save(raw,format="PNG")
        with patch("pintan.media_pipeline._ocr",return_value="材质：PE\n规格：30只装"),\
             patch("pintan.media_pipeline._cutout",side_effect=ValueError("no product")):
            result=upload_asset(self.root,"P1",raw.getvalue(),"image/png","信息图")
        with session_scope(self.root) as s:
            asset=s.get(ProductAsset,result["asset_id"])
            self.assertEqual(asset.asset_type,"信息图")
            self.assertEqual(asset.ocr_text,"材质：PE\n规格：30只装")
            self.assertEqual(asset.authorization_status,"未确认")

    def test_same_uploaded_file_can_be_linked_to_another_material_need(self):
        raw=io.BytesIO();Image.new("RGB",(400,400),"white").save(raw,format="PNG")
        with patch("pintan.media_pipeline._ocr",return_value="待复核"),patch("pintan.media_pipeline._cutout",side_effect=ValueError("no product")):
            first=upload_asset(self.root,"P1",raw.getvalue(),"image/png","参数图")
            second=upload_asset(self.root,"P1",raw.getvalue(),"image/png","功能图")
        self.assertTrue(second["deduplicated"])
        self.assertEqual(first["asset_id"],second["asset_id"])
        with session_scope(self.root) as s:
            targets=metadata(s,first["asset_id"])["manual_target_asset_types"]
        self.assertEqual(targets,["功能图","参数图"])


if __name__ == "__main__":unittest.main()
