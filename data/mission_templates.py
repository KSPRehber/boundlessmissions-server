"""
data/mission_templates.py – Rich pool of KSP weekly mission templates.

Each template: (description_en, description_tr, difficulty, category)
Categories: orbital, landing, return, construction, exploration, extreme
"""

TEMPLATES = [
    # ── Easy (difficulty 1-3) ────────────────────────────────────────────────
    # Orbital
    ("Reach a stable Kerbin orbit", "Kararlı bir Kerbin yörüngesine ulaşın", 1, "orbital"),
    ("Reach a polar orbit around Kerbin", "Kerbin etrafında kutupsal yörüngeye ulaşın", 2, "orbital"),
    ("Deploy a satellite into Kerbin orbit", "Kerbin yörüngesine bir uydu yerleştirin", 2, "orbital"),
    ("Perform a suborbital flight and recover the vessel", "Yörünge altı uçuş yapın ve aracı kurtarın", 1, "orbital"),
    ("Reach a Mun flyby trajectory", "Mun yakın geçiş yörüngesine ulaşın", 2, "orbital"),
    ("Achieve orbit with a spaceplane (SSTO to LKO)", "Bir uzay uçağıyla yörüngeye ulaşın (SSTO ile LKO)", 3, "orbital"),
    ("Dock two vessels in Kerbin orbit", "Kerbin yörüngesinde iki aracı kenetleyin", 3, "orbital"),
    ("Deploy a relay satellite network (3 sats) in Kerbin orbit", "Kerbin yörüngesine 3 röle uydusu yerleştirin", 3, "orbital"),
    ("Reach a Minmus flyby trajectory", "Minmus yakın geçiş yörüngesine ulaşın", 2, "orbital"),
    ("Perform an EVA in Kerbin orbit", "Kerbin yörüngesinde EVA yapın", 2, "orbital"),

    # Landing Easy
    ("Land on the Mun and plant a flag", "Mun'a iniş yapın ve bayrak dikin", 3, "landing"),
    ("Land on Minmus and collect surface samples", "Minmus'a iniş yapın ve yüzey örnekleri toplayın", 3, "landing"),
    ("Land a rover on the Mun", "Mun'a bir gezici indirin", 3, "landing"),
    ("Perform a crewed Mun landing and return safely", "Mürettebatlı Mun inişi yapın ve güvenle dönün", 3, "landing"),

    # ── Medium (difficulty 4-6) ───────────────────────────────────────────────
    # Return missions
    ("Land on Minmus, collect science, and return to Kerbin", "Minmus'a iniş yapın, bilim toplayın ve Kerbin'e dönün", 4, "return"),
    ("Perform a Mun landing and return with at least 3 crew", "En az 3 mürettebatla Mun'a iniş yapın ve dönün", 4, "return"),
    ("Land on Duna and return to Kerbin", "Duna'ya iniş yapın ve Kerbin'e dönün", 6, "return"),
    ("Perform an asteroid redirect mission to Kerbin orbit", "Bir asteroidi Kerbin yörüngesine yönlendirin", 5, "return"),
    ("Land on Ike and return to Kerbin", "Ike'a iniş yapın ve Kerbin'e dönün", 5, "return"),
    ("Visit both Mun and Minmus in a single mission and return", "Tek görevde hem Mun hem Minmus'u ziyaret edip dönün", 5, "return"),

    # Construction
    ("Build a space station with at least 3 modules in Kerbin orbit", "Kerbin yörüngesinde en az 3 modüllü uzay istasyonu kurun", 5, "construction"),
    ("Build a Mun surface base with at least 2 modules", "Mun yüzeyinde en az 2 modüllü üs kurun", 5, "construction"),
    ("Construct an orbital fuel depot around Kerbin", "Kerbin çevresinde yörünge yakıt deposu inşa edin", 4, "construction"),
    ("Build a mining operation on Minmus", "Minmus'ta bir madencilik operasyonu kurun", 5, "construction"),
    ("Assemble a large interplanetary ship in orbit", "Yörüngede büyük bir gezegenlerarası gemi monte edin", 6, "construction"),
    ("Deploy a communication relay network around the Mun", "Mun etrafında iletişim röle ağı kurun", 4, "construction"),

    # Exploration Medium
    ("Orbit Eve and return to Kerbin", "Eve yörüngesine girin ve Kerbin'e dönün", 5, "exploration"),
    ("Land on Gilly and return", "Gilly'ye iniş yapın ve dönün", 5, "exploration"),
    ("Perform a flyby of Jool", "Jool yakın geçişi yapın", 4, "exploration"),
    ("Orbit Dres and return", "Dres yörüngesine girin ve dönün", 5, "exploration"),
    ("Land on Duna with a rover and drive 5km", "Duna'ya gezici indirin ve 5km sürün", 5, "exploration"),
    ("Send a probe to every inner planet (Moho, Eve, Kerbin, Duna)", "Her iç gezegene sonda gönderin (Moho, Eve, Kerbin, Duna)", 6, "exploration"),

    # ── Hard (difficulty 7-8) ─────────────────────────────────────────────────
    ("Land on Tylo and return to Kerbin", "Tylo'ya iniş yapın ve Kerbin'e dönün", 7, "return"),
    ("Land on Laythe and return to Kerbin", "Laythe'e iniş yapın ve Kerbin'e dönün", 7, "return"),
    ("Complete the Jool-5 challenge (land on all 5 Jool moons)", "Jool-5 görevini tamamlayın (5 Jool uydusuna iniş)", 8, "exploration"),
    ("Land on Moho and return to Kerbin", "Moho'ya iniş yapın ve Kerbin'e dönün", 7, "return"),
    ("Build a fully operational colony on Duna with ISRU", "Duna'da ISRU ile tam operasyonel koloni kurun", 8, "construction"),
    ("Build a self-sustaining Mun base with mining and refueling", "Madencilik ve yakıt ikmali ile kendi kendine yeten Mun üssü kurun", 7, "construction"),
    ("Land on Eve's surface (no return required)", "Eve yüzeyine iniş yapın (dönüş gerekli değil)", 7, "landing"),
    ("Perform a crewed Duna landing and return", "Mürettebatlı Duna inişi ve dönüşü yapın", 7, "return"),
    ("Build a space station around Jool", "Jool etrafında uzay istasyonu kurun", 7, "construction"),
    ("Land a rover on Laythe", "Laythe'e bir gezici indirin", 7, "landing"),
    ("Establish a fuel depot in Duna orbit", "Duna yörüngesinde yakıt deposu kurun", 7, "construction"),
    ("Perform a grand tour flyby of all planets", "Tüm gezegenlerin yakın geçişini yapın", 8, "exploration"),
    ("Send a crewed mission to Eeloo and return", "Eeloo'ya mürettebatlı görev gönderin ve dönün", 8, "return"),
    ("Build an orbital shipyard around Minmus", "Minmus etrafında yörünge tersanesi kurun", 7, "construction"),

    # ── Extreme (difficulty 9-10) ─────────────────────────────────────────────
    ("Return from Eve's surface to Kerbin", "Eve yüzeyinden Kerbin'e dönün", 10, "extreme"),
    ("Complete a grand tour landing on every body and returning", "Her gök cismine iniş yaparak büyük tur tamamlayın", 10, "extreme"),
    ("Eve sea-level SSTO to orbit and return to Kerbin", "Eve deniz seviyesinden SSTO ile yörüngeye ve Kerbin'e dönün", 10, "extreme"),
    ("Colonize Laythe with a self-sustaining base", "Laythe'de kendi kendine yeten üs ile kolonileştirin", 9, "extreme"),
    ("Build an interstellar vessel and reach another star system", "Yıldızlararası gemi inşa edin ve başka yıldız sistemine ulaşın", 9, "extreme"),
    ("Complete a Jool-5 mission with a single-stage vehicle", "Tek kademeli araçla Jool-5 görevini tamamlayın", 10, "extreme"),
    ("Land on every body in the Kerbol system in a single mission", "Tek görevde Kerbol sistemindeki her gök cismine iniş yapın", 10, "extreme"),
    ("Build a fully crewed colony on Tylo", "Tylo'da tam mürettebatlı koloni kurun", 9, "extreme"),
    ("Perform a propulsive landing on Eve and return using ISRU", "Eve'de itici güçle iniş yapın ve ISRU kullanarak dönün", 10, "extreme"),
    ("Complete a stock propeller Eve ascent", "Stok pervane ile Eve'den yükselme yapın", 9, "extreme"),
]
