"""
data/mission_templates.py – Rich pool of KSP weekly mission templates.

Each template: (description_en, description_tr, difficulty, category)
Categories: orbital, landing, return, construction, exploration, extreme
"""

TEMPLATES = [
    # ── Easy (difficulty 1-3) ────────────────────────────────────────────────
    # Orbital
    ('Reach a stable Kerbin orbit', 'Kararlı bir Kerbin yörüngesine ulaşın', 1, 'orbital'),
    ('Reach a polar orbit around Kerbin', 'Kerbin etrafında kutupsal yörüngeye ulaşın', 2, 'orbital'),
    ('Deploy a satellite into Kerbin orbit', 'Kerbin yörüngesine bir uydu yerleştirin', 2, 'orbital'),
    ('Perform a suborbital flight and recover the vessel', 'Yörünge altı uçuş yapın ve aracı kurtarın', 1, 'orbital'),
    ('Reach a Mun flyby trajectory', 'Mun yakın geçiş yörüngesine ulaşın', 2, 'orbital'),
    ('Achieve orbit with a spaceplane (SSTO to LKO)', 'Bir uzay uçağıyla yörüngeye ulaşın (SSTO ile LKO)', 3, 'orbital'),
    ('Dock two vessels in Kerbin orbit', 'Kerbin yörüngesinde iki aracı kenetleyin', 3, 'orbital'),
    ('Deploy a relay satellite network (3 sats) in Kerbin orbit', 'Kerbin yörüngesine 3 röle uydusu yerleştirin', 3, 'orbital'),
    ('Reach a Minmus flyby trajectory', 'Minmus yakın geçiş yörüngesine ulaşın', 2, 'orbital'),
    ('Perform an EVA in Kerbin orbit', 'Kerbin yörüngesinde EVA yapın', 2, 'orbital'),

    # Landing Easy
    ('Land on the Mun and plant a flag', "Mun'a iniş yapın ve bayrak dikin", 3, 'landing'),
    ('Land on Minmus and collect surface samples', "Minmus'a iniş yapın ve yüzey örnekleri toplayın", 3, 'landing'),
    ('Land a rover on the Mun', "Mun'a bir gezici indirin", 3, 'landing'),
    ('Perform a crewed Mun landing and return safely', 'Mürettebatlı Mun inişi yapın ve güvenle dönün', 3, 'landing'),

    # ── Medium (difficulty 4-6) ───────────────────────────────────────────────
    # Return missions
    ('Land on Minmus, collect science, and return to Kerbin', "Minmus'a iniş yapın, bilim toplayın ve Kerbin'e dönün", 4, 'return'),
    ('Perform a Mun landing and return with at least 3 crew', "En az 3 mürettebatla Mun'a iniş yapın ve dönün", 4, 'return'),
    ('Land on Duna and return to Kerbin', "Duna'ya iniş yapın ve Kerbin'e dönün", 6, 'return'),
    ('Perform an asteroid redirect mission to Kerbin orbit', 'Bir asteroidi Kerbin yörüngesine yönlendirin', 5, 'return'),
    ('Land on Ike and return to Kerbin', "Ike'a iniş yapın ve Kerbin'e dönün", 5, 'return'),
    ('Visit both Mun and Minmus in a single mission and return', "Tek görevde hem Mun hem Minmus'u ziyaret edip dönün", 5, 'return'),

    # Construction
    ('Build a space station with at least 3 modules in Kerbin orbit', 'Kerbin yörüngesinde en az 3 modüllü uzay istasyonu kurun', 5, 'construction'),
    ('Build a Mun surface base with at least 2 modules', 'Mun yüzeyinde en az 2 modüllü üs kurun', 5, 'construction'),
    ('Construct an orbital fuel depot around Kerbin', 'Kerbin çevresinde yörünge yakıt deposu inşa edin', 4, 'construction'),
    ('Build a mining operation on Minmus', "Minmus'ta bir madencilik operasyonu kurun", 5, 'construction'),
    ('Assemble a large interplanetary ship in orbit', 'Yörüngede büyük bir gezegenlerarası gemi monte edin', 6, 'construction'),
    ('Deploy a communication relay network around the Mun', 'Mun etrafında iletişim röle ağı kurun', 4, 'construction'),

    # Exploration Medium
    ('Orbit Eve and return to Kerbin', "Eve yörüngesine girin ve Kerbin'e dönün", 5, 'exploration'),
    ('Land on Gilly and return', "Gilly'ye iniş yapın ve dönün", 5, 'exploration'),
    ('Perform a flyby of Jool', 'Jool yakın geçişi yapın', 4, 'exploration'),
    ('Orbit Dres and return', 'Dres yörüngesine girin ve dönün', 5, 'exploration'),
    ('Land on Duna with a rover and drive 5km', "Duna'ya gezici indirin ve 5km sürün", 5, 'exploration'),
    ('Send a probe to every inner planet (Moho, Eve, Kerbin, Duna)', 'Her iç gezegene sonda gönderin (Moho, Eve, Kerbin, Duna)', 6, 'exploration'),

    # ── Hard (difficulty 7-8) ─────────────────────────────────────────────────
    ('Land on Tylo and return to Kerbin', "Tylo'ya iniş yapın ve Kerbin'e dönün", 7, 'return'),
    ('Land on Laythe and return to Kerbin', "Laythe'e iniş yapın ve Kerbin'e dönün", 7, 'return'),
    ('Complete the Jool-5 challenge (land on all 5 Jool moons)', 'Jool-5 görevini tamamlayın (5 Jool uydusuna iniş)', 8, 'exploration'),
    ('Land on Moho and return to Kerbin', "Moho'ya iniş yapın ve Kerbin'e dönün", 7, 'return'),
    ('Build a fully operational colony on Duna with ISRU', "Duna'da ISRU ile tam operasyonel koloni kurun", 8, 'construction'),
    ('Build a self-sustaining Mun base with mining and refueling', 'Madencilik ve yakıt ikmali ile kendi kendine yeten Mun üssü kurun', 7, 'construction'),
    ("Land on Eve's surface (no return required)", 'Eve yüzeyine iniş yapın (dönüş gerekli değil)', 7, 'landing'),
    ('Perform a crewed Duna landing and return', 'Mürettebatlı Duna inişi ve dönüşü yapın', 7, 'return'),
    ('Build a space station around Jool', 'Jool etrafında uzay istasyonu kurun', 7, 'construction'),
    ('Land a rover on Laythe', "Laythe'e bir gezici indirin", 7, 'landing'),
    ('Complete a grand tour landing on every body and returning', 'Her gök cismine iniş yaparak büyük tur tamamlayın', 10, 'extreme'),
    ('Eve sea-level SSTO to orbit and return to Kerbin', "Eve deniz seviyesinden SSTO ile yörüngeye ve Kerbin'e dönün", 10, 'extreme'),
    ('Colonize Laythe with a self-sustaining base', "Laythe'de kendi kendine yeten üs ile kolonileştirin", 9, 'extreme'),
    ('Build an interstellar vessel and reach another star system', 'Yıldızlararası gemi inşa edin ve başka yıldız sistemine ulaşın', 9, 'extreme'),
    ('Complete a Jool-5 mission with a single-stage vehicle', 'Tek kademeli araçla Jool-5 görevini tamamlayın', 10, 'extreme'),
    ('Land on every body in the Kerbol system in a single mission', 'Tek görevde Kerbol sistemindeki her gök cismine iniş yapın', 10, 'extreme'),
    ('Build a fully crewed colony on Tylo', "Tylo'da tam mürettebatlı koloni kurun", 9, 'extreme'),
    ('Perform a propulsive landing on Eve and return using ISRU', "Eve'de itici güçle iniş yapın ve ISRU kullanarak dönün", 10, 'extreme'),
    ('Complete a stock propeller Eve ascent', "Stok pervane ile Eve'den yükselme yapın", 9, 'extreme'),
    ('[Kcalbeloh] Send a crewed station to Kcalbeloh Orbit', '[Kcalbeloh] Kcalbeloh yörüngesine insanlı bir istasyon görevi gönderin', 10, 'extreme'),
    
    # ── Mod: Outer Planets Mod (OPM) ──────────────────────────────────────────
    ('[Outer Planets Mod] Perform a flyby of Sarnus', '[Outer Planets Mod] Sarnus yakın geçişi yapın', 5, 'exploration'),
    ('[Outer Planets Mod] Land on Tekto and return', "[Outer Planets Mod] Tekto'ya iniş yapın ve dönün", 7, 'return'),
    ('[Outer Planets Mod] Deploy a relay network around Urlum', '[Outer Planets Mod] Urlum etrafında röle ağı kurun', 6, 'construction'),
    ('[Outer Planets Mod] Perform a crewed landing on Slate', "[Outer Planets Mod] Slate'e mürettebatlı iniş yapın", 8, 'landing'),
    ('[Outer Planets Mod] Orbit Neidon and return to Kerbin', "[Outer Planets Mod] Neidon yörüngesine girin ve Kerbin'e dönün", 7, 'return'),
    ('[Outer Planets Mod] Land a rover on Plock', "[Outer Planets Mod] Plock'a gezici indirin", 7, 'landing'),
    ('[Outer Planets Mod] Perform a grand tour of all Outer Planets Mod planets', '[Outer Planets Mod] Tüm Outer Planets Mod gezegenlerini kapsayan büyük bir tur yapın', 10, 'extreme'),
    ('[Outer Planets Mod] Build a refueling station in Sarnus orbit', '[Outer Planets Mod] Sarnus yörüngesinde yakıt istasyonu kurun', 7, 'construction'),
    ("[Outer Planets Mod] Send a probe into Jool's lower atmosphere and survive", "[Outer Planets Mod] Jool'un alt atmosferine sonda gönderin ve hayatta kalın", 8, 'extreme'),
    ('[Outer Planets Mod] Return a surface sample from Hale', "[Outer Planets Mod] Hale'den yüzey örneği getirip dönün", 6, 'return'),
    
    # ── Mod: Kcalbeloh System ─────────────────────────────────────────────────
    ('[Kcalbeloh] Travel through the Kcalbeloh wormhole', '[Kcalbeloh] Kcalbeloh solucan deliğinden geçin', 7, 'exploration'),
    ('[Kcalbeloh] Orbit the Kcalbeloh black hole safely', '[Kcalbeloh] Kcalbeloh kara deliği etrafında güvenli yörüngeye girin', 8, 'exploration'),
    ('[Kcalbeloh] Land on Rouqea and establish a base', "[Kcalbeloh] Rouqea'ya iniş yapıp üs kurun", 9, 'construction'),
    ('[Kcalbeloh] Perform a flyby of the binary stars', '[Kcalbeloh] İkili yıldızların yakın geçişini yapın', 8, 'exploration'),
    ('[Kcalbeloh] Return a sample from Ater', "[Kcalbeloh] Ater'den örnek getirip dönün", 9, 'return'),
    ('[Kcalbeloh] Deploy a science station in Kcalbeloh orbit', '[Kcalbeloh] Kcalbeloh yörüngesine bilim istasyonu kurun', 8, 'construction'),
    ('[Kcalbeloh] Land on Sunorc', "[Kcalbeloh] Sunorc'a iniş yapın", 8, 'landing'),
    ('[Kcalbeloh] Send a crewed mission to the Kcalbeloh system and return to Kerbin', "[Kcalbeloh] Kcalbeloh sistemine mürettebatlı görev gönderip Kerbin'e dönün", 10, 'extreme'),
    ('[Kcalbeloh] Establish a permanent colony on a Kcalbeloh planet', '[Kcalbeloh] Bir Kcalbeloh gezegeninde kalıcı koloni kurun', 10, 'extreme'),
    
    # ── Mod: Far Future Technologies ──────────────────────────────────────────
    ('[Far Future Technologies] Build an Antimatter factory in orbit', '[Far Future Technologies] Yörüngede Antimadde fabrikası kurun', 8, 'construction'),
    ('[Far Future Technologies] Reach another star using a Fusion Drive', '[Far Future Technologies] Füzyon Motoru kullanarak başka bir yıldıza ulaşın', 9, 'extreme'),
    ('[Far Future Technologies] Construct a massive interstellar generation ship', '[Far Future Technologies] Devasa bir yıldızlararası nesil gemisi inşa edin', 9, 'construction'),
    ("[Far Future Technologies] Harvest antimatter from Jool's magnetosphere", "[Far Future Technologies] Jool'un manyetosferinden antimadde toplayın", 8, 'exploration'),
    ('[Far Future Technologies] Perform a high-speed interstellar flyby', '[Far Future Technologies] Yüksek hızlı yıldızlararası yakın geçiş yapın', 9, 'exploration'),
    ('[Far Future Technologies] Deploy a laser-pumped propulsion network', '[Far Future Technologies] Lazer pompalı itki ağı kurun', 8, 'construction'),
    
    # ── Mod: Near Future Technologies ─────────────────────────────────────────
    ('[Near Future Technologies] Build a nuclear-powered tug for orbital construction', '[Near Future Technologies] Yörünge inşası için nükleer güçle çalışan römorkör yapın', 5, 'construction'),
    ('[Near Future Technologies] Deploy a large solar array station in low Kerbol orbit', '[Near Future Technologies] Alçak Kerbol yörüngesine büyük güneş paneli istasyonu kurun', 6, 'construction'),
    ('[Near Future Technologies] Perform an ion-drive only mission to Eeloo', "[Near Future Technologies] Sadece iyon motoru ile Eeloo'ya görev yapın", 6, 'exploration'),
    ('[Near Future Technologies] Construct a base using Near Future Construction parts', '[Near Future Technologies] Near Future Construction parçalarıyla üs kurun', 4, 'construction'),
    ('[Near Future Technologies] Use Argon gas propulsion for a Duna transfer', '[Near Future Technologies] Duna transferi için Argon gazı itkisi kullanın', 5, 'exploration'),
    
    # ── Mod: Kerbalism / USI Life Support ─────────────────────────────────────
    ('[Kerbalism / USI Life Support] Keep a Kerbal alive in space for 10 years continuously', "[Kerbalism / USI Life Support] Bir Kerbal'ı uzayda 10 yıl boyunca kesintisiz hayatta tutun", 7, 'extreme'),
    ('[Kerbalism / USI Life Support] Build a fully self-sufficient greenhouse base on Duna', "[Kerbalism / USI Life Support] Duna'da tamamen kendi kendine yeten sera üssü kurun", 8, 'construction'),
    ('[Kerbalism / USI Life Support] Survive a severe solar storm in interplanetary space', '[Kerbalism / USI Life Support] Gezegenlerarası uzayda şiddetli bir güneş fırtınasından sağ kurtulun', 6, 'extreme'),
    ('[Kerbalism / USI Life Support] Establish a USI MKS logistics hub in Mun orbit', '[Kerbalism / USI Life Support] Mun yörüngesinde USI MKS lojistik merkezi kurun', 7, 'construction'),
    ('[Kerbalism / USI Life Support] Set up a planetary resource extraction chain', '[Kerbalism / USI Life Support] Gezegensel kaynak çıkarma zinciri kurun', 7, 'construction'),
    
    # ── Real Solar System (Real Solar System) / RO ──────────────────────────────────────────
    ('[Real Solar System] Reach Earth Orbit in Real Solar System', "[Real Solar System] Real Solar System'te Dünya Yörüngesine ulaşın", 5, 'orbital'),
    ('[Real Solar System] Perform a Moon landing in Real Solar System', "[Real Solar System] Real Solar System'te Ay inişi yapın", 7, 'landing'),
    ('[Real Solar System] Land a rover on Mars in Real Solar System', "[Real Solar System] Real Solar System'te Mars'a gezici indirin", 8, 'landing'),
    ('[Real Solar System] Perform a crewed Apollo-style mission in Real Solar System', "[Real Solar System] Real Solar System'te Apollo tarzı insanlı görev yapın", 8, 'return'),
    ('[Real Solar System] Send a probe to Jupiter in Real Solar System', "[Real Solar System] Real Solar System'te Jüpiter'e sonda gönderin", 7, 'exploration'),
    ('[Real Solar System] Land on Venus in Real Solar System', "[Real Solar System] Real Solar System'te Venüs'e iniş yapın", 8, 'landing'),
    ('[Real Solar System] Send a Voyager-style probe out of the solar system in Real Solar System', "[Real Solar System] Real Solar System'te güneş sistemi dışına Voyager tarzı sonda gönderin", 9, 'extreme'),
    ('[Real Solar System] Perform a crewed Mars landing and return in Real Solar System', "[Real Solar System] Real Solar System'te insanlı Mars inişi ve dönüşü yapın", 10, 'extreme'),
    ('[Real Solar System] Build the ISS in Earth orbit in Real Solar System', "[Real Solar System] Real Solar System'te Dünya yörüngesinde ISS'i inşa edin", 8, 'construction'),
    
    # ── More Base Game & Creative Scenarios ───────────────────────────────────
    ("Rescue a Kerbal from Eve's surface", "Eve yüzeyinden bir Kerbal'ı kurtarın", 10, 'extreme'),
    ('Capture a Class E asteroid and land it on Kerbin safely', "E Sınıfı bir asteroidi yakalayıp Kerbin'e güvenle indirin", 9, 'extreme'),
    ("Build a submarine and explore Laythe's oceans", 'Bir denizaltı yapıp Laythe okyanuslarını keşfedin', 7, 'exploration'),
    ('Build a helicopter and fly it on Duna', "Bir helikopter yapıp Duna'da uçurun", 7, 'exploration'),
    ('Perform a precision landing on the VAB helipad from orbit', 'Yörüngeden VAB helikopter pistine hassas iniş yapın', 6, 'landing'),
    ('Construct a Mun arch research base', 'Mun kemeri araştırma üssü kurun', 5, 'construction'),
    ('Fly an SSTO to Minmus, refuel, and go to Duna', "Minmus'a SSTO uçurun, yakıt alıp Duna'ya gidin", 8, 'extreme'),
    ('Use a gravity assist from Eve to reach Jool', "Jool'a ulaşmak için Eve'den kütleçekim sapması (gravity assist) kullanın", 6, 'exploration'),
    ('Perform a Kerbol (Sun) dive under 1,000,000 km', "Kerbol'a (Güneş) 1,000,000 km altına dalış yapın", 7, 'extreme'),
    ('Land on the Mohole on Moho', "Moho'daki Mohole'a (kutuplardaki dev çukur) iniş yapın", 8, 'landing'),
    ('Build a rover that can drive upside down', 'Ters dönebilen bir gezici araç yapın', 3, 'exploration'),
    ("Recover a splashed down capsule from Kerbin's ocean using a boat", 'Kerbin okyanusuna düşmüş bir kapsülü tekne ile kurtarın', 4, 'exploration'),
    ('Deploy a constellation of 10 satellites in a single launch', 'Tek fırlatmada 10 uyduluk bir ağ kurun', 5, 'orbital'),
    ('Build a functional space tether/elevator concept', 'Çalışan bir uzay asansörü/bağlantısı konsepti inşa edin', 9, 'extreme'),
    ('Create an orbital ring around Minmus', 'Minmus etrafında yörüngesel bir halka inşa edin', 8, 'construction'),
    ('Perform a lithobraking (crash-landing) survival mission', 'Litofrenleme (çarpma ile yavaşlama) yaparak hayatta kalın', 5, 'landing'),
    ('Fly through the R&D bridge with a jet', 'Bir jet ile Ar-Ge köprüsünün altından uçun', 4, 'exploration'),
    ('Build a mech/walker and walk 1km on Kerbin', "Bir mecha/yürüyen robot yapıp Kerbin'de 1km yürütün", 6, 'exploration'),
    ('Deploy a constellation of 10 satellites in a single launch', 'Tek fırlatmada 10 uyduluk bir ağ kurun', 5, 'orbital'),
    ('Build a functional space tether/elevator concept', 'Çalışan bir uzay asansörü/bağlantısı konsepti inşa edin', 9, 'extreme'),
    ('Create an orbital ring around Minmus', 'Minmus etrafında yörüngesel bir halka inşa edin', 8, 'construction'),
    ('Perform a lithobraking (crash-landing) survival mission', 'Litofrenleme (çarpma ile yavaşlama) yaparak hayatta kalın', 5, 'landing'),
    ('Fly through the R&D bridge with a jet', 'Bir jet ile Ar-Ge köprüsünün altından uçun', 4, 'exploration'),
    ('Build a mech/walker and walk 1km on Kerbin', "Bir mecha/yürüyen robot yapıp Kerbin'de 1km yürütün", 6, 'exploration')
]
