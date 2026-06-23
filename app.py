from flask import Flask, render_template, request, jsonify, session, redirect, url_for
import sqlite3
import hashlib
import secrets
from dotenv import load_dotenv
from google import genai
from google.genai import types
import os
import json
import stripe

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", secrets.token_hex(32))
STRIPE_SECRET_KEY    = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_PUB_KEY       = os.getenv("STRIPE_PUBLISHABLE_KEY", "")
BASE_URL             = os.getenv("BASE_URL", "http://localhost:5000")
stripe.api_key       = STRIPE_SECRET_KEY
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("RENDER", False)
app.config["SESSION_COOKIE_HTTPONLY"] = True

# ── Database ───────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        email TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        credits INTEGER DEFAULT 1,
        plan TEXT DEFAULT 'free',
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS generations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        property TEXT,
        output TEXT,
        layout TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
    conn.commit()
    conn.close()

def get_user(email):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT * FROM users WHERE email=?", (email,))
    row = c.fetchone()
    conn.close()
    return {"id":row[0],"email":row[1],"password":row[2],"credits":row[3],"plan":row[4]} if row else None

def create_user(email, password):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    hashed = hashlib.sha256(password.encode()).hexdigest()
    try:
        c.execute("INSERT INTO users (email,password,credits) VALUES (?,?,1)", (email, hashed))
        conn.commit(); conn.close(); return True
    except sqlite3.IntegrityError:
        conn.close(); return False

def deduct_credit(user_id):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("UPDATE users SET credits=credits-1 WHERE id=? AND credits>0", (user_id,))
    updated = c.rowcount
    conn.commit(); conn.close()
    return updated > 0

def save_generation(user_id, prop, output, layout):
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("INSERT INTO generations (user_id,property,output,layout) VALUES (?,?,?,?)",
              (user_id, json.dumps(prop), json.dumps(output), layout))
    conn.commit(); conn.close()

# Run on every startup including Gunicorn
init_db()


GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ── Layout picker ──────────────────────────────────────────────────────────────
def pick_layout(prop):
    price    = prop.get("price", 0)
    audience = prop.get("audience", "").lower()
    tone     = prop.get("tone", "").lower()
    ptype    = prop.get("property_type", "").lower()

    if tone == "urgent":                          return "urgent"
    if "arab" in audience:                        return "bilingual"
    if price >= 3000000 or tone == "luxury":      return "luxury-dark"
    if "investor" in audience:                    return "stats-heavy"
    if "villa" in ptype or "townhouse" in ptype:  return "magazine"
    if "family" in audience:                      return "neighbourhood"
    if price < 1000000:                           return "bold-block"
    return "clean-white"


AREA_FACTS = {
    "dubai marina": "Dubai Marina is a 3.5km waterfront promenade with 200+ restaurants, JBR beach 5 min walk, Dubai Marina Mall, Metro at DMCC station, yacht clubs, and stunning canal views. Known for young professionals and expats.",
    "downtown dubai": "Downtown Dubai has Burj Khalifa (world's tallest), Dubai Mall (world's largest), Dubai Fountain, Dubai Opera, easy Metro access at Burj Khalifa/Dubai Mall station. Premium central location.",
    "palm jumeirah": "Palm Jumeirah is a man-made island with private beaches, Atlantis hotel, Nakheel Mall, Palm Monorail, luxury villas and apartments with panoramic sea views. Ultra-premium address.",
    "business bay": "Business Bay is Dubai's business hub with canal views, proximity to Downtown (5 min), Metro at Business Bay station, Bay Avenue mall, trendy cafes and co-working spaces. Growing young professional community.",
    "jumeirah village circle": "JVC (Jumeirah Village Circle) is an affordable family-friendly community with Circle Mall, parks, community feel, easy access to Sheikh Mohammed Bin Zayed Road. Popular with value-seekers.",
    "jvc": "JVC (Jumeirah Village Circle) is an affordable family-friendly community with Circle Mall, parks, community feel, easy access to Sheikh Mohammed Bin Zayed Road. Popular with value-seekers.",
    "dubai hills estate": "Dubai Hills Estate has Dubai Hills Mall, 18-hole golf course, Dubai Hills Park (largest in Dubai), top schools nearby (GEMS, King's College Hospital adjacent). Master-planned green community.",
    "arabian ranches": "Arabian Ranches is a premium villa community with Ranches Souk, Arabian Ranches Golf Club, top schools (Jumeirah English Speaking School), equestrian centre. Established family favourite.",
    "jumeirah lake towers": "JLT (Jumeirah Lake Towers) has scenic lake views, 80+ F&B options, Metro at DMCC station, proximity to Dubai Marina, affordable compared to Marina. Popular with professionals.",
    "difc": "DIFC (Dubai International Financial Centre) is Dubai's financial hub with Gate Avenue retail, world-class restaurants, art galleries, and direct access to Dubai's business elite. Premium address.",
    "dubai silicon oasis": "Dubai Silicon Oasis is a tech-focused free zone community with Silicon Central Mall, affordable rents, family-friendly environment, easy access to Academic City universities. Good value.",
    "city walk": "City Walk is an open-air lifestyle destination with 300+ retail and dining outlets, Coca-Cola Arena, Green Planet biodome, close to Downtown and DIFC. Trendy urban living.",
    "sobha hartland": "Sobha Hartland is a waterfront green community on Dubai Canal with Hartland International School, North London Collegiate School Dubai, lush landscaping. Premium family community.",
    "creek harbour": "Dubai Creek Harbour has Dubai Creek Tower (under construction, taller than Burj Khalifa), Creek Beach, The Viewing Point, stunning Downtown skyline views. Future-focused investment.",
    "motor city": "Motor City has a racing circuit heritage, Green Community, retail strip, Union Properties developments, quiet suburban feel. Affordable with strong community vibe.",
    "town square": "Town Square Dubai has one of the largest community parks in Dubai (2.4 million sqft), outdoor cinema, splash pads, dog park, and retail strip. Affordable family living.",
    "mirdif": "Mirdif is an established suburb with Mirdif City Centre mall, Mushrif Park, family-friendly villas and apartments, close to Dubai International Airport. Community feel.",
    "al barsha": "Al Barsha is centrally located with Mall of the Emirates (Ski Dubai), Metro on Red Line, diverse F&B options, mix of villas and apartments. Practical urban living.",
    "discovery gardens": "Discovery Gardens is an affordable community near Ibn Battuta Mall, Metro access at Ibn Battuta station, themed clusters, popular with budget-conscious residents.",
    "international city": "International City has the lowest rents in Dubai, Dragon Mart (largest Chinese trading hub outside China), themed country clusters. Ultra-affordable entry point.",
    "damac hills": "DAMAC Hills has Trump International Golf Club Dubai, Malibu Beach wave pool, horse stables, community retail. Established golf community with good amenities.",
    "mudon": "Mudon is a villa community by Dubai Properties with Mudon Central Park, retail centre, sports facilities, close to Arabian Ranches. Family-focused suburban living.",
    "jumeirah golf estates": "Jumeirah Golf Estates has two championship golf courses (Earth and Fire), luxury villas, close to Expo City, good schools nearby. Premium golf lifestyle.",
    "tilal al ghaf": "Tilal Al Ghaf by Majid Al Futtaim has Lagoon Al Ghaf (largest crystal lagoon in Dubai), beach clubs, family-friendly amenities. New premium community.",
    "the springs": "The Springs is an Emaar villa community with community lakes, Springs Souk, established greenery, good schools nearby. Consistently popular family community.",
    "dubai harbour": "Dubai Harbour has the largest marina in the MENA region, Skydive Dubai, Bluewaters Island adjacent, stunning sea views. Premium new waterfront address.",
    "emaar beachfront": "Emaar Beachfront is a private island community with 1.5km private beach, close to Dubai Marina and JBR, limited units. Exclusive beachfront living.",
    "bur dubai": "Bur Dubai is the historic heart of Dubai with Dubai Museum, Al Fahidi Heritage Area, Meena Bazaar, excellent Metro access, diverse dining. Cultural and affordable.",
    "deira": "Deira is Dubai's trading hub with Gold Souk, Spice Souk, Deira City Centre, waterfront Corniche, excellent Metro access. Authentic Dubai living at affordable prices.",
}

def get_area_facts(community):
    key = community.lower().strip()
    # Direct match
    if key in AREA_FACTS:
        return AREA_FACTS[key]
    # Partial match
    for area, facts in AREA_FACTS.items():
        if area in key or key in area:
            return facts
    return f"{community} is a well-established Dubai community known for its lifestyle amenities and strong property market."

# ── AI generation ──────────────────────────────────────────────────────────────
def generate_content(prop):
    layout = pick_layout(prop)

    if not client:
        return get_sample_output(prop), layout

    try:
        p   = prop["price"]
        s   = int(str(prop.get("size", "1000")).replace(",", ""))
        psf = round(p / max(s, 1))
        rent = round(p * 0.065 / 1000) * 1000

        prompt = f"""You are a Dubai real estate social media expert.
Generate a 7-day Instagram content plan for this property.

PROPERTY:
- Community: {prop['community']}
- Area Facts: {get_area_facts(prop['community'])}
- Type: {prop['beds']} bedroom {prop['property_type']}
- Size: {prop.get('size','1000')} sqft | Price: AED {p:,} | AED {psf:,}/sqft
- Est. annual rent: AED {rent:,} | Gross yield: ~6.5%
- Features: {prop['features']}
- Target Audience: {prop['audience']}
- Tone: {prop['tone']}
- Agent: {prop['agent_name']}
- Listing Type: {prop['listing_type'].replace('-',' ').title()}
- Layout style: {layout}

Layout guide:
luxury-dark = ultra-premium, minimal, powerful
clean-white = friendly, modern, approachable
bold-block  = punchy, short, high energy
magazine    = editorial, rich descriptions, aspirational
stats-heavy = lead with numbers, yield, ROI, psf
neighbourhood = community lifestyle, connectivity, warmth
urgent      = FOMO, scarcity, time pressure, strong CTA
bilingual   = English caption + key lines in Arabic

Day angles:
Day 1=Property Showcase, Day 2=Investment ROI, Day 3=Lifestyle,
Day 4=Price Value, Day 5=Call to Action, Day 6=Area Highlights, Day 7=Urgency

If FOR RENT: focus captions on lifestyle, monthly cost, availability, tenant benefits.
If FOR SALE: focus on investment ROI, capital appreciation, ownership benefits.

Return ONLY valid JSON, no extra text:
{{
  "posts": [
    {{
      "day": 1,
      "angle": "Property Showcase",
      "caption": "full caption here",
      "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
      "best_time": "8:00 AM",
      "emoji_hook": "✨",
      "layout_note": "visual tip for this post"
    }},
    {{
      "day": 2,
      "angle": "Investment ROI",
      "caption": "full caption here",
      "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
      "best_time": "12:00 PM",
      "emoji_hook": "📊",
      "layout_note": "visual tip"
    }},
    {{
      "day": 3,
      "angle": "Lifestyle",
      "caption": "full caption here",
      "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
      "best_time": "6:00 PM",
      "emoji_hook": "🌅",
      "layout_note": "visual tip"
    }},
    {{
      "day": 4,
      "angle": "Price Value",
      "caption": "full caption here",
      "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
      "best_time": "10:00 AM",
      "emoji_hook": "💡",
      "layout_note": "visual tip"
    }},
    {{
      "day": 5,
      "angle": "Call to Action",
      "caption": "full caption here",
      "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
      "best_time": "7:00 PM",
      "emoji_hook": "🔑",
      "layout_note": "visual tip"
    }},
    {{
      "day": 6,
      "angle": "Area Highlights",
      "caption": "full caption here",
      "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
      "best_time": "9:00 AM",
      "emoji_hook": "📍",
      "layout_note": "visual tip"
    }},
    {{
      "day": 7,
      "angle": "Urgency",
      "caption": "full caption here",
      "hashtags": ["tag1","tag2","tag3","tag4","tag5","tag6","tag7","tag8","tag9","tag10"],
      "best_time": "11:00 AM",
      "emoji_hook": "⏰",
      "layout_note": "visual tip"
    }}
  ],
  "stories": [
    {{"day": 1, "idea": "story idea"}},
    {{"day": 3, "idea": "story idea"}},
    {{"day": 5, "idea": "story idea"}}
  ],
  "whatsapp": "broadcast message 60-80 words",
  "linkedin": "professional post 120-150 words"
}}"""

        response = client.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt
        )
        text = response.text.strip()
        if "```" in text:
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
        return json.loads(text.strip()), layout

    except Exception as e:
        print(f"Gemini error: {type(e).__name__}: {e}")
        return get_sample_output(prop), layout

# ── Sample output (fallback) ───────────────────────────────────────────────────
# Replace get_sample_output in app.py with this version
def get_sample_output(prop):
    name     = prop.get("agent_name", "Your Name")
    comm     = prop.get("community", "Dubai Marina")
    beds     = prop.get("beds", "2")
    p        = prop.get("price", 1500000)
    s        = int(str(prop.get("size", "1000")).replace(",", ""))
    psf      = round(p / max(s, 1))
    rent     = round(p * 0.065 / 1000) * 1000
    is_rent  = prop.get("listing_type", "for-sale") == "for-rent"

    # For rent: price IS the annual rent. For sale: price is sale price.
    price_display = f"AED {p:,}/yr (AED {round(p/12):,}/mo)" if is_rent else f"AED {p:,}"
    day2_caption  = (
        f"🏠 Why rent in {comm}?\n\n"
        f"✅ Fully managed building\n"
        f"✅ Flexible lease terms\n"
        f"✅ Prime location\n"
        f"✅ Move-in ready\n\n"
        f"💰 {price_display}\n"
        f"📐 {prop.get('size','1,000')} sqft\n\n"
        f"Limited units available. DM {name} to book a viewing."
        if is_rent else
        f"📊 The numbers:\n\n{beds}BR in {comm}\n"
        f"Price: AED {p:,}\nPrice/sqft: AED {psf:,}\n"
        f"Est. annual rent: AED {rent:,}\nGross yield: ~6.5%\n\n"
        f"Zero tax. Strong Golden Visa pathway.\n\nDM {name} for a full ROI breakdown."
    )

    return {
        "posts": [
            {"day":1,"angle":"Property Showcase","emoji_hook":"🏠" if is_rent else "✨",
             "best_time":"8:00 AM","layout_note":"Use a hero image of the living room or view",
             "caption":(
                f"🏠 Available for Rent: {beds}BR in {comm}\n\n"
                f"Your next home is waiting.\n\n"
                f"💰 {price_display}\n"
                f"📐 {prop.get('size','1,000')} sqft\n"
                f"📍 {comm}, Dubai\n\n"
                f"DM {name} to arrange a viewing."
                if is_rent else
                f"✨ Just Listed: {beds}BR in {comm}\n\n"
                f"This exceptional property offers everything Dubai living has to offer.\n\n"
                f"💰 AED {p:,}\n"
                f"📐 {prop.get('size','1,000')} sqft (AED {psf:,}/sqft)\n"
                f"📍 {comm}, Dubai\n\n"
                f"DM {name} for a private viewing."
             ),
             "hashtags":["DubaiRental" if is_rent else "DubaiRealEstate",
                        "DubaiProperty",comm.replace(' ',''),
                        "DubaiLiving","UAEProperty","DubaiHomes",
                        "RentDubai" if is_rent else "DubaiInvestment",
                        "DubaiApartments" if is_rent else "LuxuryDubai",
                        "DubaiLife","UAELiving"]},
            {"day":2,"angle":"Why Rent Here" if is_rent else "Investment ROI",
             "emoji_hook":"🏡" if is_rent else "📊",
             "best_time":"12:00 PM","layout_note":"Highlight key benefits",
             "caption": day2_caption,
             "hashtags":["DubaiRental","RentInDubai","DubaiApartments",
                        "UAELiving","DubaiLiving","HomeDubai",
                        "DubaiProperty","RentDubai","DubaiLife","MoveToDubai"]
                        if is_rent else
                        ["DubaiInvestment","PropertyROI","DubaiYield","UAEInvestor",
                        "GoldenVisa","TaxFree","DubaiRealEstate","InvestDubai",
                        "PropertyInvestment","PassiveIncome"]},
            {"day":3,"angle":"Lifestyle","emoji_hook":"🌅","best_time":"6:00 PM",
             "layout_note":"Lifestyle image — pool, view, or community",
             "caption":(
                f"🌅 Imagine coming home to this every evening.\n\n"
                f"Renting in {comm} means world-class dining, beaches, "
                f"and a global community at your doorstep.\n\n"
                f"This {beds}BR is available now at {price_display}.\n\n"
                f"Tag someone who should live here 👇\n\nDM {name} to arrange a viewing."
                if is_rent else
                f"🌅 This is what your mornings could look like.\n\n"
                f"Life in {comm} means world-class dining, beaches, "
                f"and a global community at your doorstep.\n\n"
                f"This {beds}BR at AED {p:,} is a lifestyle upgrade.\n\n"
                f"Tag someone who deserves this 👇\n\nDM {name} to arrange a viewing."
             ),
             "hashtags":["DubaiLifestyle",comm.replace(' ',''),"DubaiLiving",
                        "LuxuryLifestyle","DubaiVibes","UAELife",
                        "DubaiDaily","LivingInDubai","DubaiDreams","DubaiLife"]},
            {"day":4,"angle":"Value" if is_rent else "Price Value",
             "emoji_hook":"💡","best_time":"10:00 AM",
             "layout_note":"Highlight value vs other areas",
             "caption":(
                f"💡 What AED {round(p/12):,}/month gets you in {comm}.\n\n"
                f"{beds}BR | {prop.get('size','1,000')} sqft\n"
                f"✅ No agency fees on renewal\n"
                f"✅ All amenities included\n"
                f"✅ Flexible payment cheques\n\n"
                f"Compare this to similar units elsewhere — {comm} offers exceptional value.\n\n"
                f"DM {name} for details."
                if is_rent else
                f"💡 Smart buyers are watching {comm} right now.\n\n"
                f"At AED {psf:,}/sqft this {beds}BR represents strong value.\n\n"
                f"✅ Premium location\n✅ Strong rental demand\n✅ Motivated seller\n\n"
                f"AED {p:,} — DM {name} before someone else does."
             ),
             "hashtags":["DubaiRental","AffordableDubai","DubaiValue"] if is_rent else
                        ["DubaiPropertyMarket","ValueBuy","SmartInvesting",
                        "PropertyValue","UAEProperty","DubaiHomes","BuyInDubai",
                        "DubaiDeals","MarketInsight","DubaiRealEstate"]},
            {"day":5,"angle":"Call to Action","emoji_hook":"🔑","best_time":"7:00 PM",
             "layout_note":"Bold CTA",
             "caption":(
                f"🔑 Ready to move in?\n\n"
                f"{beds}BR | {comm} | {price_display}\n\n"
                f"📅 Available immediately\n"
                f"📞 DM {name} NOW to book a viewing\n"
                f"✈️ Virtual tour available for overseas tenants\n"
                f"📋 Simple tenancy process\n\n"
                f"Don't miss this one."
                if is_rent else
                f"🔑 Your move, Dubai.\n\n"
                f"{beds}BR | {comm} | AED {p:,}\n\n"
                f"Multiple inquiries this week. Now is the time.\n\n"
                f"📞 DM {name} NOW\n📅 Viewing slots this week only\n"
                f"✈️ Virtual tour available"
             ),
             "hashtags":["RentNow","DubaiRental","AvailableNow","DubaiApartments",
                        "HomeDubai","MoveToDubai","DubaiProperty","UAELiving",
                        "DubaiLife","DubaiHomes"] if is_rent else
                        ["DubaiPropertyForSale","BuyPropertyDubai","DubaiAgent",
                        "PropertyForSale","ActNow","DubaiHomes","MoveToDubai",
                        "DubaiDream","UAERealEstate","DubaiAgents"]},
            {"day":6,"angle":"Area Highlights","emoji_hook":"📍","best_time":"9:00 AM",
             "layout_note":"Area map or landmark photo",
             "caption":(
                f"📍 Why tenants love {comm}.\n\n"
                f"{get_area_facts(comm)}\n\n"
                f"🛍️ World-class retail nearby\n"
                f"🍽️ International dining scene\n"
                f"🚇 Great connectivity\n"
                f"🌍 Vibrant expat community\n\n"
                f"This {beds}BR at {price_display} puts you right in the middle of it.\n\n"
                f"DM {name} for details."
                if is_rent else
                f"📍 Why {comm} keeps topping the list.\n\n"
                f"{get_area_facts(comm)}\n\n"
                f"🍽️ International dining\n🚇 Great connectivity\n"
                f"📈 Consistent price growth\n\n"
                f"{beds}BR at AED {p:,}.\n\nDM {name} for details."
             ),
             "hashtags":[comm.replace(' ',''),"DubaiAreas","WhereToLiveInDubai",
                        "DubaiCommunities","UAELiving","DubaiGuide",
                        "LifeInDubai","DubaiNeighbourhood","DubaiLocal","PropertyLocation"]},
            {"day":7,"angle":"Urgency","emoji_hook":"⏰","best_time":"11:00 AM",
             "layout_note":"Urgency — limited availability",
             "caption":(
                f"⏰ This won't last long.\n\n"
                f"{beds}BR in {comm} — {price_display}\n\n"
                f"I've had multiple viewing requests this week.\n\n"
                f"Send me 'RENT' right now and I'll get you a viewing within 24 hours.\n\n"
                f"— {name} 📲"
                if is_rent else
                f"⏰ Final push — {beds}BR in {comm} at AED {p:,}.\n\n"
                f"Seller is motivated. I need this sold this week.\n\n"
                f"Send 'VIEWING' right now and I'll get you in within 24 hours.\n\n"
                f"Serious buyers only.\n\n— {name} 📲"
             ),
             "hashtags":["UrgentRental","DubaiRental","LastUnit","DubaiProperty",
                        "RentNow","AvailableNow","DubaiHomes","ActNow",
                        "DubaiLife","MoveToDubai"] if is_rent else
                        ["UrgentSale","DubaiPropertyForSale","MotivatedSeller",
                        "DubaiRealEstate","PropertyDeal","LimitedOffer",
                        "DontMissOut","DubaiHomes","ActNow","WeekendDeal"]},
        ],
        "stories": [
            {"day":1,"idea":f"{'Property walkthrough: show each room + monthly price reveal at end.' if is_rent else '5-6 swipe photos ending with price reveal. Add DM for viewing sticker.'}"},
            {"day":3,"idea":f"Poll: 'Would you rent in {comm}?' Yes 🔥 / Still looking 👀" if is_rent else f"Poll: 'Would you live in {comm}?' Yes 🔥 / Still looking 👀"},
            {"day":5,"idea":f"Countdown: 'Unit available for X more days — book your viewing now.'" if is_rent else f"Countdown: 'Viewing slots filling up — only X left this week.'"},
        ],
        "whatsapp": (
            f"🏠 Rental Alert — {comm}\n\n"
            f"{beds}BR | {price_display} | {prop.get('size','1,000')} sqft\n\n"
            f"Well-maintained unit in a prime location. Available immediately.\n\n"
            f"Reply YES for full details, photos, and viewing appointment.\n\n"
            f"— {name} | Dubai Real Estate"
            if is_rent else
            f"🏙️ Property Alert — {comm}\n\n"
            f"{beds}BR | AED {p:,} | {prop.get('size','1,000')} sqft\n\n"
            f"Exceptional unit with strong investment fundamentals.\n\n"
            f"Reply YES for full details and viewing appointment.\n\n"
            f"— {name} | Dubai Real Estate"
        ),
        "linkedin": (
            f"Excited to present this {beds}-bedroom rental in {comm}, Dubai.\n\n"
            f"Available at {price_display} — an excellent option for professionals "
            f"and families relocating to Dubai.\n\n"
            f"{comm} continues to be one of Dubai's most sought-after residential communities, "
            f"offering strong lifestyle credentials and convenient connectivity.\n\n"
            f"Reach out if you or someone you know is looking for quality rental accommodation in Dubai.\n\n"
            f"#DubaiRental #DubaiRealEstate #Dubai #{comm.replace(' ','')} #UAELiving"
            if is_rent else
            f"Presenting this exceptional {beds}-bedroom property in {comm}, Dubai.\n\n"
            f"Listed at AED {p:,} (AED {psf:,}/sqft).\n\n"
            f"Key metrics:\n• Est. gross yield: ~6.5%\n"
            f"• Est. annual rent: AED {rent:,}\n"
            f"• {'Golden Visa eligible' if p >= 2000000 else 'Strong entry-level investment'}\n\n"
            f"Reach out for a detailed investment analysis.\n\n"
            f"#DubaiRealEstate #PropertyInvestment #Dubai #{comm.replace(' ','')}"
        )
    }

# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    user = None
    if "user_id" in session:
        conn = sqlite3.connect("users.db")
        c = conn.cursor()
        c.execute("SELECT email, credits FROM users WHERE id=?", (session["user_id"],))
        row = c.fetchone()
        conn.close()
        if row:
            user = {"email": row[0], "credits": row[1]}
    return render_template("index.html", user=user)

@app.route("/generate", methods=["POST"])
def generate():
    if "user_id" not in session:
        return jsonify({"error": "Please sign up or log in", "auth_required": True}), 401

    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE id=?", (session["user_id"],))
    row = c.fetchone()
    conn.close()
    if not row or row[0] <= 0:
        return jsonify({"error": "No credits remaining", "no_credits": True}), 402

    data = request.get_json()
    prop = {
        "agent_name":    data.get("agent_name", ""),
        "community":     data.get("community", ""),
        "listing_type":  data.get("listing_type", "for-sale"),
        "property_type": data.get("property_type", "Apartment"),
        "beds":          data.get("beds", "2"),
        "size":          data.get("size", "1000"),
        "price":         int(str(data.get("price", "1500000")).replace(",", "")),
        "features":      data.get("features", ""),
        "audience":      data.get("audience", "Investors"),
        "tone":          data.get("tone", "Professional"),
    }

    if not prop["community"] or not prop["agent_name"]:
        return jsonify({"error": "Community and agent name are required"}), 400

    if not deduct_credit(session["user_id"]):
        return jsonify({"error": "No credits remaining", "no_credits": True}), 402

    output, layout = generate_content(prop)
    save_generation(session["user_id"], prop, output, layout)
    return jsonify({"success": True, "output": output, "layout": layout})

@app.route("/signup", methods=["POST"])
def signup():
    d = request.get_json()
    email = d.get("email", "").strip().lower()
    pwd = d.get("password", "")
    if not email or not pwd:
        return jsonify({"error": "Email and password required"}), 400
    if len(pwd) < 6:
        return jsonify({"error": "Password must be at least 6 characters"}), 400
    if create_user(email, pwd):
        user = get_user(email)
        session["user_id"] = user["id"]
        print(f"Session after signup: {session}")
        return jsonify({"success": True, "credits": 1})
    return jsonify({"error": "Email already registered"}), 400

@app.route("/login", methods=["POST"])
def login():
    d = request.get_json()
    email = d.get("email", "").strip().lower()
    pwd = d.get("password", "")
    hashed = hashlib.sha256(pwd.encode()).hexdigest()
    user = get_user(email)
    if user and user["password"] == hashed:
        session["user_id"] = user["id"]
        return jsonify({"success": True, "credits": user["credits"]})
    return jsonify({"error": "Invalid email or password"}), 401

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("index"))

@app.route("/credits")
def get_credits():
    if "user_id" not in session:
        return jsonify({"credits": 0})
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT credits FROM users WHERE id=?", (session["user_id"],))
    row = c.fetchone()
    conn.close()
    return jsonify({"credits": row[0]}) if row else jsonify({"credits": 0})

@app.route("/create-checkout", methods=["POST"])
def create_checkout():
    if "user_id" not in session:
        return jsonify({"error": "Not logged in"}), 401
    data = request.get_json()
    packages = {
        "starter":   {"credits": 30,  "price": 9900,  "name": "Starter — 30 Posts"},
        "pro":       {"credits": 100, "price": 29900, "name": "Pro — 100 Posts"},
        "unlimited": {"credits": 500, "price": 99900, "name": "Agency — 500 Posts"},
    }
    pkg = packages.get(data.get("package", "starter"), packages["starter"])
    conn = sqlite3.connect("users.db")
    c = conn.cursor()
    c.execute("SELECT email FROM users WHERE id=?", (session["user_id"],))
    row = c.fetchone()
    conn.close()
    email = row[0] if row else ""
    try:
        checkout = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price_data": {"currency": "aed",
                         "unit_amount": pkg["price"],
                         "product_data": {"name": pkg["name"]}},
                         "quantity": 1}],
            mode="payment",
            success_url=f"{BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{BASE_URL}/",
            customer_email=email,
            metadata={"user_email": email, "credits": str(pkg["credits"])},
        )
        return jsonify({"checkout_url": checkout.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/success")
def success():
    session_id = request.args.get("session_id")
    if session_id:
        try:
            co = stripe.checkout.Session.retrieve(session_id)
            if co.payment_status == "paid":
                email   = co.metadata.get("user_email")
                credits = int(co.metadata.get("credits", 30))
                conn = sqlite3.connect("users.db")
                c = conn.cursor()
                c.execute("UPDATE users SET credits=credits+?, plan='paid' WHERE email=?",
                          (credits, email))
                conn.commit()
                conn.close()
        except Exception:
            pass
    return render_template("success.html")


if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5000)

from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)    