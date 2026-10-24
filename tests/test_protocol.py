import json
from pathlib import Path
import unittest
from unittest.mock import Mock

from app.catalog import bind_prompt, normalize_request
from app.client import ToonkitClient, build_music_body, _authorized_media_url, _request_ulid


class ProtocolTests(unittest.TestCase):
    def test_request_id_is_crockford_ulid(self):
        value = _request_ulid()
        self.assertEqual(len(value), 26)
        self.assertTrue(set(value) <= set("0123456789ABCDEFGHJKMNPQRSTVWXYZ"))

    def test_media_aliases_use_upstream_mentions(self):
        values = {"images": ["x", "y"], "videos": ["z"], "audios": ["a"]}
        text = bind_prompt("图1 img2 video1 音频1", values)
        self.assertEqual(text, "@图片1 @图片2 @视频1 @音频1")

    def test_video_limits_match_live_capability(self):
        body = {"model": "sd-2-5", "prompt": "reference @图片1", "duration": 30,
                "image_urls": ["https://example.test/a.png"] * 30,
                "video_urls": ["https://example.test/a.mp4"] * 10,
                "audio_urls": ["https://example.test/a.mp3"] * 10}
        self.assertEqual(normalize_request(body)["upstream_model"], "SEEDANCE_2_5")
        with self.assertRaises(ValueError):
            normalize_request({"model": "minimax-h3-768p", "prompt": "test", "duration": 5,
                               "video_urls": ["https://example.test/a.mp4"]})

    def test_music_custom_protocol(self):
        request = normalize_request({"model": "suno-v6", "custom_mode": True,
            "title": "A song", "lyrics": "Hello", "tags": "pop", "prompt": "summer"})
        body = build_music_body(request, {"images": [], "videos": [], "audios": []})
        self.assertEqual(body["mode"], "CUSTOM")
        self.assertEqual(body["style"], "pop")
        self.assertEqual(body["title"], "A song")
        self.assertNotIn("prompt", body)

    def test_completed_status_falls_back_to_media_listing(self):
        client = ToonkitClient({"cdp_port": 1, "proxy_url": "socks5://127.0.0.1:20022"})
        client.browser = Mock()
        client.browser.get.side_effect = [
            {"id": "a", "status": "COMPLETED", "outputAudioUrl": None},
            {"items": [{"id": "a", "outputAudioUrl": "https://cdn.example.test/audio.mp3"}]},
        ]
        result = client.poll("generation", json.dumps(["a"]))
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(len(result["result_urls"]), 1)

    def test_standard_suno_custom_alias_preserves_full_lyrics(self):
        lyrics = "line\n" * 900
        request = normalize_request({"model": "suno-v6", "customMode": "true",
                                     "title": "A song", "prompt": lyrics, "style": "pop"})
        body = build_music_body(request, {"images": [], "videos": [], "audios": []})
        self.assertEqual(body["mode"], "CUSTOM")
        self.assertEqual(body["lyrics"], lyrics.strip())
        self.assertEqual(body["style"], "pop")
        self.assertNotIn("prompt", body)

    def test_string_false_is_not_custom_or_instrumental(self):
        request = normalize_request({"model": "suno-v6", "prompt": "short music",
                                     "custom_mode": "false", "make_instrumental": "false"})
        body = build_music_body(request, {"images": [], "videos": [], "audios": []})
        self.assertEqual(body["mode"], "DEFAULT")
        self.assertFalse(body["instrumental"])
        self.assertEqual(body["prompt"], "short music")

    def test_default_description_supplies_a_counted_style_attachment(self):
        description = "A warm acoustic folk song about a quiet morning by a lake."
        request = normalize_request({"model": "suno-v6", "prompt": description})
        body = build_music_body(request, {"images": [], "videos": [], "audios": []})
        self.assertEqual(body["mode"], "DEFAULT")
        self.assertEqual(body["style"], description)
        self.assertEqual(body["prompt"], description)
        self.assertNotIn("attachments", body)

    def test_default_payload_satisfies_captured_official_attachment_contract(self):
        capabilities=json.loads((Path(__file__).parent/"fixtures/toonkit_music_capabilities.json").read_text())
        limits=capabilities["limits"]
        self.assertTrue(limits["defaultRequiresStyleLyricsOrReference"])
        self.assertTrue(limits["styleAndLyricsCountAsAttachments"])
        self.assertEqual(limits["styleMaxLength"],1000)
        for model in ("suno-v6","suno-v6-mini","suno-v6-wild"):
            with self.subTest(model=model):
                body=build_music_body(normalize_request({"model":model,"prompt":"Warm acoustic folk song"}),
                                      {"images":[],"videos":[],"audios":[]})
                counted=sum(len(body[k]) for k in ("referenceImageIds","referenceVideoIds","referenceAudioIds"))+bool(body["style"])+bool(body["lyrics"])
                self.assertGreaterEqual(counted,1)
                self.assertLessEqual(counted,limits["maxAttachments"])
                self.assertLessEqual(len(body["style"]),limits["styleMaxLength"])

    def test_default_explicit_style_lyrics_and_reference_inputs_are_preserved(self):
        for extra,assets in (({"tags":"folk"},{"images":[],"videos":[],"audios":[]}),
                             ({"lyrics":"Exact verse"},{"images":[],"videos":[],"audios":[]}),
                             ({"image_urls":["toonkit:ref"]},{"images":[{"toonkit_asset_id":"ref"}],"videos":[],"audios":[]})):
            with self.subTest(extra=extra):
                request=normalize_request({"model":"suno-v6","prompt":"instruction",**extra})
                body=build_music_body(request,assets)
                self.assertEqual(body["style"],extra.get("tags",""))
                self.assertEqual(body["lyrics"],extra.get("lyrics",""))
                self.assertEqual(body["referenceImageIds"],["ref"] if assets["images"] else [])

    def test_default_counted_text_and_media_limits_fail_before_submission(self):
        self.assertEqual(build_music_body(normalize_request({"model":"suno-v6","prompt":"x"*1000}),
                                         {"images":[],"videos":[],"audios":[]})["style"],"x"*1000)
        with self.assertRaisesRegex(ValueError,"style limit"):
            normalize_request({"model":"suno-v6","prompt":"x"*1001})
        # A real reference satisfies the minimum without copying a long
        # instruction into the smaller style field.
        normalize_request({"model":"suno-v6","prompt":"x"*3000,"audio_urls":["toonkit:a"]})
        normalize_request({"model":"suno-v6","tags":"folk","audio_urls":["toonkit:a"]*9})
        with self.assertRaisesRegex(ValueError,"1-10"):
            normalize_request({"model":"suno-v6","tags":"folk","lyrics":"verse","audio_urls":["toonkit:a"]*9})

    def test_custom_instrumental_needs_title_but_no_lyrics(self):
        request = normalize_request({"model": "suno-v6", "customMode": True,
                                     "title": "Instrumental", "instrumental": True, "tags": "jazz"})
        self.assertEqual(request["lyrics"], "")
        self.assertTrue(request["make_instrumental"])

    def test_private_cdn_uses_session_token(self):
        url = _authorized_media_url("https://private-cdn.toonkit.io/user/video.mp4", "session-token")
        self.assertIn("token=session-token", url)


if __name__ == "__main__":
    unittest.main()
