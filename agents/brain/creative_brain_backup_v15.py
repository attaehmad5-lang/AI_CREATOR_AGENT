class CreativeBrain:
    def __init__(self):
        self.status = "READY"
        self.version = "1.0"
        self.categories = ["film","cinematic_story","storytelling","documentary","history","education","music","music_video","entertainment","shorts","reels","tiktok","advertisement","commercial","explainer","creative_visual","travel","culture","science","technology","research","fact_checking","news_information","podcast","interview","tutorial","course","presentation","report","document","pdf","proposal","communication","social_media","content_calendar","creator_management","business_management","project_management","general_creative"]

    def analyze(self, request):
        request = request or {}
        text = str(request.get("prompt", "") or request.get("topic", "")).lower()
        detected = request.get("type", "auto")
        keyword_map = {"music_video":["song","music video","گانا","موسیقی","گانے کی ویڈیو","گانے کی وڈیو"],"music":["music","audio song","موسیقی"],"film":["film","movie","فلم","مووی"],"documentary":["documentary","دستاویزی"],"education":["education","learn","course","tutorial","تعلیم"],"advertisement":["ad","advertisement","اشتہار","تشہیر"],"research":["research","تحقیق"],"fact_checking":["fact check","verify facts","حقائق"],"pdf":["pdf","pdf report","پی ڈی ایف"],"report":["report","رپورٹ"],"proposal":["proposal","پروپوزل"],"communication":["message","reply","communication","پیغام","جواب"],"social_media":["social media","social post","سوشل میڈیا"],"shorts":["shorts","youtube short"],"reels":["reels","instagram reel"],"tiktok":["tiktok"],"creator_management":["creator management","creator workflow","کرییٹر مینجمنٹ"],"business_management":["business management","business workflow","کاروباری مینجمنٹ"],"project_management":["project management","project plan","پروجیکٹ مینجمنٹ","پروجیکٹ پلان","پروجیکٹ پلان بناؤ"]}
        if detected == "auto":
            for category, keywords in keyword_map.items():
                if any(k in text for k in keywords):
                    detected = category
                    break

        workflow_registry = {"film":"FILM_WORKFLOW","cinematic_story":"CINEMATIC_STORY_WORKFLOW","storytelling":"STORYTELLING_WORKFLOW","documentary":"DOCUMENTARY_WORKFLOW","history":"HISTORY_WORKFLOW","education":"EDUCATION_WORKFLOW","music":"MUSIC_WORKFLOW","music_video":"MUSIC_VIDEO_WORKFLOW","entertainment":"ENTERTAINMENT_WORKFLOW","shorts":"SHORTS_WORKFLOW","reels":"REELS_WORKFLOW","tiktok":"TIKTOK_WORKFLOW","advertisement":"ADVERTISEMENT_WORKFLOW","commercial":"COMMERCIAL_WORKFLOW","explainer":"EXPLAINER_WORKFLOW","creative_visual":"CREATIVE_VISUAL_WORKFLOW","travel":"TRAVEL_WORKFLOW","culture":"CULTURE_WORKFLOW","science":"SCIENCE_WORKFLOW","technology":"TECHNOLOGY_WORKFLOW","research":"RESEARCH_WORKFLOW","fact_checking":"FACT_CHECKING_WORKFLOW","news_information":"NEWS_INFORMATION_WORKFLOW","podcast":"PODCAST_WORKFLOW","interview":"INTERVIEW_WORKFLOW","tutorial":"TUTORIAL_WORKFLOW","course":"COURSE_WORKFLOW","presentation":"PRESENTATION_WORKFLOW","report":"REPORT_WORKFLOW","document":"DOCUMENT_WORKFLOW","pdf":"PDF_WORKFLOW","proposal":"PROPOSAL_WORKFLOW","communication":"COMMUNICATION_WORKFLOW","social_media":"SOCIAL_MEDIA_WORKFLOW","content_calendar":"CONTENT_CALENDAR_WORKFLOW","creator_management":"CREATOR_MANAGEMENT_WORKFLOW","business_management":"BUSINESS_MANAGEMENT_WORKFLOW","project_management":"PROJECT_MANAGEMENT_WORKFLOW","general_creative":"GENERAL_CREATIVE_WORKFLOW"}; workflow = workflow_registry.get(detected,"GENERAL_CREATIVE_WORKFLOW")
        plan_profiles = {"research":{"research":True,"idea":True,"script":True,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"fact_checking":{"research":True,"idea":False,"script":True,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"pdf":{"research":True,"idea":True,"script":True,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"report":{"research":True,"idea":True,"script":True,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"document":{"research":True,"idea":True,"script":True,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"communication":{"research":False,"idea":False,"script":True,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"project_management":{"research":True,"idea":True,"script":False,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"creator_management":{"research":True,"idea":True,"script":False,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"business_management":{"research":True,"idea":True,"script":False,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"content_calendar":{"research":True,"idea":True,"script":False,"storyboard":False,"visual_generation":False,"audio":False,"subtitles":False,"editing":False,"quality_control":True},"presentation":{"research":True,"idea":True,"script":True,"storyboard":True,"visual_generation":True,"audio":False,"subtitles":False,"editing":False,"quality_control":True}}; default_plan={"research":True,"idea":True,"script":True,"storyboard":True,"visual_generation":True,"audio":True,"subtitles":True,"editing":True,"quality_control":True}; production_plan=plan_profiles.get(detected,default_plan)
        topic = str(request.get("topic", "") or "").strip()
        if not topic:
            raw = str(request.get("prompt", "") or "").strip()
            for marker in ["کے بارے میں", "بارے میں", "پر"]:
                if marker in raw:
                    topic = raw.split(marker, 1)[0].strip(" ۔,،")
                    break
        goal = "general_request"
        if any(x in text for x in ["بناؤ", "بنائیں", "create", "make", "تیار کرو", "تیار کریں"]):
            goal = "create"
        elif any(x in text for x in ["چیک کرو", "چیک کریں", "verify", "fact check", "تحقیق کرو"]):
            goal = "verify"
        elif any(x in text for x in ["لکھو", "لکھیں", "write", "draft"]):
            goal = "write"
        language = str(request.get("language", "") or "").strip()
        if not language:
            urdu_chars = sum(1 for ch in text if "؀" <= ch <= "ۿ")
            latin_chars = sum(1 for ch in text if ch.isascii() and ch.isalpha())
            language = "ur" if urdu_chars > 0 else "en" if latin_chars > 0 else "auto"
        platform = "general"
        platform_terms = ["youtube", "یوٹیوب", "tiktok", "ٹک ٹاک", "instagram", "انسٹاگرام", "facebook", "فیس بک", "shorts", "شارٹس", "reels", "ریلز"]
        for term in platform_terms:
            if term in text:
                platform = term
                break
        output_format = "default"
        output_terms = ["pdf", "پی ڈی ایف", "mp4", "video", "ویڈیو", "image", "تصویر", "audio", "آڈیو", "script", "اسکرپٹ", "report", "رپورٹ", "presentation", "پریزنٹیشن"]
        for term in output_terms:
            if term in text:
                output_format = term
                break
        audience = "general"
        audience_terms = ["children", "بچوں", "kids", "نوجوان", "youth", "family", "فیملی", "professional audience", "پروفیشنل", "students", "طلبہ", "عام audience", "عام ناظرین"]
        for term in audience_terms:
            if term in text:
                audience = term
                break
        style = "default"
        style_terms = ["cinematic", "سینیمیٹک", "sad", "درد بھرا", "emotional", "جذباتی", "professional", "پروفیشنل", "premium", "پریمیم"]
        for term in style_terms:
            if term in text:
                style = term
                break
        return {"status":"READY","topic":topic,"language":language,"content_type":detected,"goal":goal,"style":style,"platform":platform,"output_format":output_format,"audience":audience,"category_detection":"COMPLETED","workflow_selection":workflow,"production_plan":production_plan}
