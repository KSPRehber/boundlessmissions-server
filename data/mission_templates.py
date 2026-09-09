"""
data/mission_templates.py – The weekly-mission pool.

Each template is a 7-tuple:

    (desc_en, desc_tr, difficulty, category,
     mission_type, required_situation, required_body)

The last three used to be *inferred* — `api_server._classify_missions` asked
Gemini (or, without it, `_classify_heuristic`) what each week's twenty missions
required, and cached the answer. They are authored here instead, and that is the
whole point of this file's current shape: a weekly mission is written by us, so
what the submit gate will demand of it is a fact we can write down rather than a
guess a model has to re-derive every week. Authoring it also costs one Firestore
read and one AI call less per week, but that is the bonus, not the reason.

The reason is that the guess was wrong often enough to ship missions **nobody
could submit**, and a bot-issued contract that cannot be submitted still carries a
fine and a due date. Three of those failures are worth naming, because they are
what every entry below is checked against:

  • `"Perform a suborbital flight and recover the vessel"` — the heuristic tests
    `"orbit" in text`, and `"suborbital"` contains it, so a *suborbital* mission
    demanded `ORBITING`. And even classified correctly it is unsubmittable: the
    mission ends with the vessel recovered, and a recovered vessel is not an
    active vessel, so there is nothing left to submit from.
  • `"Land on Duna and return to Kerbin"` — body extraction walks
    `settings.KNOWN_CELESTIAL_BODIES` in list order and takes the first name that
    appears in the text. `Kerbin` precedes `Duna` there, so the contract required
    a craft landed on *Kerbin*. A round trip has no single moment that shows it
    either way: at Duna the return has not happened, and back at Kerbin nothing in
    the snapshot says the craft was ever at Duna.
  • `"[Kcalbeloh] Land on Rouqea"` — the bracketed mod tag is matched by the same
    body scan, so the required body came out `Kcalbeloh` and a correct landing on
    Rouqea was refused for being at the wrong body.

## What "supported" means here

`SubmissionSession` (KSP side) and `_bot_mission_evidence_problem` (server side)
both judge **one moment**, and they judge the same one:

  • `craft_build` — the player is in the VAB/SPH. Evidence is the `.craft` file,
    the used-part list and a render.
  • `active_vessel` — the player is in flight with one vessel. Evidence is that
    vessel's telemetry: `body`, `situation`, apoapsis/periapsis/inclination/
    eccentricity, latitude/longitude/altitude, crew count and traits, part count,
    mass. Plus screenshots, which the AI reviewer reads alongside it.

There is no history in that snapshot. It cannot say where the craft has been, how
far it drove, how long a kerbal has been alive, or that a vessel now recovered
once existed. So a template earns its place only if there is a moment when the
player is in the editor with the craft, or in flight with one vessel, such that
that moment both **satisfies** the body/situation gate and **is** the evidence.

That rules four kinds of mission out, and they are gone from this file:

  1. **Recovery** — the end state is "no vessel" (`…and recover the vessel`).
  2. **Round trips** — `…and return to Kerbin`. Rewritten to end at the
     destination, which is the half a snapshot can actually show.
  3. **Multi-target** — Jool-5, grand tours, `visit both Mun and Minmus`,
     `a probe to every inner planet`. One snapshot, one place.
  4. **Processes and durations** — `drive 5 km`, `walk 1 km`, `fly through the
     R&D bridge`, `keep a Kerbal alive for 10 years`, `survive a solar storm`,
     `use a gravity assist from Eve`. The snapshot records a state, never an
     event that led to it.

## Authoring rules

  • `mission_type` is `"craft_build"` or `"active_vessel"`.
  • `required_situation` is one of KSP's own situation strings — `ORBITING`,
    `LANDED`, `SPLASHED`, `FLYING`, `SUB_ORBITAL`, `ESCAPING` — or `None`.
    `DOCKED` is deliberately never used: docking merges two vessels and the
    result reports `ORBITING`, so requiring it would refuse every successful
    docking. Docking missions ask for `ORBITING` and are judged on the
    screenshot and the part count.
  • `required_body` must be a name from `settings.KNOWN_CELESTIAL_BODIES`, or
    `None`. A body outside that list is one nothing else in the system can
    resolve, which is why the Kcalbeloh entries name only bodies that are in it.
  • `craft_build` templates carry `None` for both — an editor submission has no
    situation and no body.
  • The text is still read by `data/mission_constraints.extract_heuristic` and
    `data/orbit_constraints.extract_heuristic`, which is a feature: "with at
    least 3 crew aboard" becomes an enforced crew floor, "polar orbit" an
    enforced inclination, "using argon-fuelled ion propulsion" an enforced
    propellant and engine category. Word new entries with that in mind — a
    number in the text can become a bound.

Categories are presentation only: orbital, landing, return, construction,
exploration, extreme.
"""

# (desc_en, desc_tr, difficulty, category, mission_type, required_situation, required_body)
TEMPLATES = [
    # ── Easy (difficulty 1-3) ────────────────────────────────────────────────
    ('Reach a stable orbit around Kerbin',
     'Kerbin etrafında kararlı bir yörüngeye ulaşın',
     1, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Reach space on a suborbital trajectory above Kerbin',
     'Kerbin üzerinde yörünge altı bir rotayla uzaya ulaşın',
     1, 'orbital', 'active_vessel', 'SUB_ORBITAL', 'Kerbin'),
    ("Splash down in Kerbin's ocean",
     'Kerbin okyanusuna suya iniş yapın',
     1, 'landing', 'active_vessel', 'SPLASHED', 'Kerbin'),
    ('Fly an aircraft above 10 km over Kerbin',
     'Kerbin üzerinde 10 km yüksekliğin üzerinde bir uçak uçurun',
     1, 'exploration', 'active_vessel', 'FLYING', 'Kerbin'),
    ('Reach a polar orbit around Kerbin',
     'Kerbin etrafında kutupsal yörüngeye ulaşın',
     2, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Deploy a satellite into Kerbin orbit',
     'Kerbin yörüngesine bir uydu yerleştirin',
     2, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Perform an EVA in Kerbin orbit',
     'Kerbin yörüngesinde EVA yapın',
     2, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ("Enter the Mun's sphere of influence on a flyby trajectory",
     "Mun'un etki alanına yakın geçiş rotasıyla girin",
     2, 'orbital', 'active_vessel', 'ESCAPING', 'Mun'),
    ("Enter Minmus's sphere of influence on a flyby trajectory",
     "Minmus'un etki alanına yakın geçiş rotasıyla girin",
     2, 'orbital', 'active_vessel', 'ESCAPING', 'Minmus'),
    ('Achieve orbit with a spaceplane (SSTO to low Kerbin orbit)',
     'Bir uzay uçağıyla yörüngeye ulaşın (SSTO ile alçak Kerbin yörüngesi)',
     3, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Dock two vessels in Kerbin orbit',
     'Kerbin yörüngesinde iki aracı kenetleyin',
     3, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Deploy three relay satellites into Kerbin orbit',
     'Kerbin yörüngesine üç röle uydusu yerleştirin',
     3, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Land on the Mun and plant a flag',
     "Mun'a iniş yapın ve bayrak dikin",
     3, 'landing', 'active_vessel', 'LANDED', 'Mun'),
    ('Land on Minmus with a scientist aboard',
     "Minmus'a bir bilim insanıyla iniş yapın",
     3, 'landing', 'active_vessel', 'LANDED', 'Minmus'),
    ('Land a rover on the Mun',
     "Mun'a bir gezici indirin",
     3, 'landing', 'active_vessel', 'LANDED', 'Mun'),
    ('Perform a crewed Mun landing',
     "Mürettebatlı bir Mun inişi yapın",
     3, 'landing', 'active_vessel', 'LANDED', 'Mun'),
    ('Build a rover that can drive upside down',
     'Ters dönmüş halde sürülebilen bir gezici tasarlayın',
     3, 'construction', 'craft_build', None, None),

    # ── Medium (difficulty 4-6) ──────────────────────────────────────────────
    ('Land on the Mun with at least 3 crew aboard',
     "Mun'a en az 3 mürettebatla iniş yapın",
     4, 'landing', 'active_vessel', 'LANDED', 'Mun'),
    ('Land on Minmus with at least 3 crew aboard',
     "Minmus'a en az 3 mürettebatla iniş yapın",
     4, 'landing', 'active_vessel', 'LANDED', 'Minmus'),
    ('Construct an orbital fuel depot around Kerbin',
     'Kerbin çevresinde yörünge yakıt deposu inşa edin',
     4, 'construction', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Deploy a communication relay network around the Mun',
     'Mun etrafında iletişim röle ağı kurun',
     4, 'construction', 'active_vessel', 'ORBITING', 'Mun'),
    ('Perform a flyby of Jool',
     'Jool yakın geçişi yapın',
     4, 'exploration', 'active_vessel', 'ESCAPING', 'Jool'),
    ("Sail a boat out to a capsule splashed down in Kerbin's ocean",
     'Kerbin okyanusuna inmiş bir kapsüle tekneyle ulaşın',
     4, 'exploration', 'active_vessel', 'SPLASHED', 'Kerbin'),
    ('Land on Ike',
     "Ike'a iniş yapın",
     5, 'landing', 'active_vessel', 'LANDED', 'Ike'),
    ('Enter orbit around Eve',
     'Eve yörüngesine girin',
     5, 'exploration', 'active_vessel', 'ORBITING', 'Eve'),
    ('Land on Gilly',
     "Gilly'ye iniş yapın",
     5, 'landing', 'active_vessel', 'LANDED', 'Gilly'),
    ('Enter orbit around Dres',
     'Dres yörüngesine girin',
     5, 'exploration', 'active_vessel', 'ORBITING', 'Dres'),
    ('Land a rover on Duna',
     "Duna'ya bir gezici indirin",
     5, 'landing', 'active_vessel', 'LANDED', 'Duna'),
    ('Capture an asteroid and bring it into Kerbin orbit',
     'Bir asteroidi yakalayıp Kerbin yörüngesine getirin',
     5, 'exploration', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Build a space station with at least 3 modules in Kerbin orbit',
     'Kerbin yörüngesinde en az 3 modüllü uzay istasyonu kurun',
     5, 'construction', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Build a Mun surface base with at least 2 modules',
     'Mun yüzeyinde en az 2 modüllü üs kurun',
     5, 'construction', 'active_vessel', 'LANDED', 'Mun'),
    ('Build a mining operation on Minmus',
     "Minmus'ta bir madencilik operasyonu kurun",
     5, 'construction', 'active_vessel', 'LANDED', 'Minmus'),
    ('Construct a research base at a Mun arch',
     'Mun kemerinin yanında bir araştırma üssü kurun',
     5, 'construction', 'active_vessel', 'LANDED', 'Mun'),
    ('Deploy a constellation of 10 satellites into Kerbin orbit from a single launch',
     'Tek fırlatmayla Kerbin yörüngesine 10 uyduluk bir ağ yerleştirin',
     5, 'orbital', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Land on Duna',
     "Duna'ya iniş yapın",
     6, 'landing', 'active_vessel', 'LANDED', 'Duna'),
    ('Assemble a large interplanetary ship in Kerbin orbit',
     'Kerbin yörüngesinde büyük bir gezegenlerarası gemi monte edin',
     6, 'construction', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('Land a returning craft on the KSC helipad',
     'Dönen bir aracı KSC helikopter pistine indirin',
     6, 'landing', 'active_vessel', 'LANDED', 'Kerbin'),
    ('Build a walking mech in the VAB',
     "VAB'de yürüyen bir mecha tasarlayın",
     6, 'construction', 'craft_build', None, None),

    # ── Hard (difficulty 7-8) ────────────────────────────────────────────────
    ('Land on Tylo',
     "Tylo'ya iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Tylo'),
    ('Land on Laythe',
     "Laythe'e iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Laythe'),
    ('Land a rover on Laythe',
     "Laythe'e bir gezici indirin",
     7, 'landing', 'active_vessel', 'LANDED', 'Laythe'),
    ('Land on Moho',
     "Moho'ya iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Moho'),
    ("Land on Eve's surface",
     'Eve yüzeyine iniş yapın',
     7, 'landing', 'active_vessel', 'LANDED', 'Eve'),
    ('Perform a crewed Duna landing',
     'Mürettebatlı bir Duna inişi yapın',
     7, 'landing', 'active_vessel', 'LANDED', 'Duna'),
    ('Build a space station in orbit around Jool',
     'Jool yörüngesinde uzay istasyonu kurun',
     7, 'construction', 'active_vessel', 'ORBITING', 'Jool'),
    ('Build a self-sustaining Mun base with mining and refuelling',
     'Madencilik ve yakıt ikmali ile kendi kendine yeten bir Mun üssü kurun',
     7, 'construction', 'active_vessel', 'LANDED', 'Mun'),
    ('Enter a low orbit around Kerbol',
     'Kerbol etrafında alçak bir yörüngeye girin',
     7, 'exploration', 'active_vessel', 'ORBITING', 'Kerbol'),
    ("Take a submersible into Laythe's oceans",
     'Laythe okyanuslarına bir denizaltı indirin',
     7, 'exploration', 'active_vessel', 'SPLASHED', 'Laythe'),
    ('Build a helicopter and fly it on Duna',
     "Bir helikopter yapıp Duna'da uçurun",
     7, 'exploration', 'active_vessel', 'FLYING', 'Duna'),
    ("Land on the Mohole at Moho's north pole",
     "Moho'nun kuzey kutbundaki Mohole'a iniş yapın",
     8, 'landing', 'active_vessel', 'LANDED', 'Moho'),
    ('Build a fully operational colony on Duna with ISRU',
     "Duna'da ISRU ile tam operasyonel bir koloni kurun",
     8, 'construction', 'active_vessel', 'LANDED', 'Duna'),
    ("Fly a probe into Jool's atmosphere",
     "Jool'un atmosferine bir sonda uçurun",
     8, 'exploration', 'active_vessel', 'FLYING', 'Jool'),
    ('Create an orbital ring around Minmus',
     'Minmus etrafında yörüngesel bir halka inşa edin',
     8, 'construction', 'active_vessel', 'ORBITING', 'Minmus'),

    # ── Extreme (difficulty 9-10) ────────────────────────────────────────────
    ('Colonize Laythe with a self-sustaining base',
     "Laythe'de kendi kendine yeten bir üsle kolonileşin",
     9, 'extreme', 'active_vessel', 'LANDED', 'Laythe'),
    ('Build a fully crewed colony on Tylo',
     "Tylo'da tam mürettebatlı bir koloni kurun",
     9, 'extreme', 'active_vessel', 'LANDED', 'Tylo'),
    ('Capture a Class E asteroid and land it on Kerbin',
     "E sınıfı bir asteroidi yakalayıp Kerbin'e indirin",
     9, 'extreme', 'active_vessel', 'LANDED', 'Kerbin'),
    ('Fly a stock propeller aircraft above 20 km on Eve',
     "Eve'de stok pervaneli bir uçağı 20 km üzerine çıkarın",
     9, 'extreme', 'active_vessel', 'FLYING', 'Eve'),
    ('Build a functional space elevator concept in the VAB',
     "VAB'de çalışan bir uzay asansörü konsepti tasarlayın",
     9, 'extreme', 'craft_build', None, None),
    ("Reach Eve orbit launching from Eve's surface",
     'Eve yüzeyinden fırlatıp Eve yörüngesine ulaşın',
     10, 'extreme', 'active_vessel', 'ORBITING', 'Eve'),

    # ── Mod: Outer Planets Mod (OPM) ─────────────────────────────────────────
    ('[Outer Planets Mod] Perform a flyby of Sarnus',
     '[Outer Planets Mod] Sarnus yakın geçişi yapın',
     5, 'exploration', 'active_vessel', 'ESCAPING', 'Sarnus'),
    ('[Outer Planets Mod] Land on Hale',
     "[Outer Planets Mod] Hale'ye iniş yapın",
     6, 'landing', 'active_vessel', 'LANDED', 'Hale'),
    ('[Outer Planets Mod] Deploy a relay network around Urlum',
     '[Outer Planets Mod] Urlum etrafında röle ağı kurun',
     6, 'construction', 'active_vessel', 'ORBITING', 'Urlum'),
    ('[Outer Planets Mod] Land on Tekto',
     "[Outer Planets Mod] Tekto'ya iniş yapın",
     7, 'landing', 'active_vessel', 'LANDED', 'Tekto'),
    ('[Outer Planets Mod] Enter orbit around Neidon',
     '[Outer Planets Mod] Neidon yörüngesine girin',
     7, 'exploration', 'active_vessel', 'ORBITING', 'Neidon'),
    ('[Outer Planets Mod] Land a rover on Plock',
     "[Outer Planets Mod] Plock'a bir gezici indirin",
     7, 'landing', 'active_vessel', 'LANDED', 'Plock'),
    ('[Outer Planets Mod] Build a refuelling station in Sarnus orbit',
     '[Outer Planets Mod] Sarnus yörüngesinde yakıt ikmal istasyonu kurun',
     7, 'construction', 'active_vessel', 'ORBITING', 'Sarnus'),
    ('[Outer Planets Mod] Perform a crewed landing on Slate',
     "[Outer Planets Mod] Slate'e mürettebatlı iniş yapın",
     8, 'landing', 'active_vessel', 'LANDED', 'Slate'),

    # ── Mod: Kcalbeloh System ────────────────────────────────────────────────
    # Only bodies that are in settings.KNOWN_CELESTIAL_BODIES are named here.
    ('[Kcalbeloh System] Enter orbit around the Kcalbeloh black hole',
     '[Kcalbeloh System] Kcalbeloh kara deliği etrafında yörüngeye girin',
     8, 'exploration', 'active_vessel', 'ORBITING', 'Kcalbeloh'),
    ('[Kcalbeloh System] Deploy a science probe into Kcalbeloh orbit',
     '[Kcalbeloh System] Kcalbeloh yörüngesine bir bilim sondası yerleştirin',
     8, 'construction', 'active_vessel', 'ORBITING', 'Kcalbeloh'),
    ('[Kcalbeloh System] Send a crewed station into Kcalbeloh orbit',
     '[Kcalbeloh System] Kcalbeloh yörüngesine mürettebatlı bir istasyon gönderin',
     10, 'extreme', 'active_vessel', 'ORBITING', 'Kcalbeloh'),

    # ── Mod: Far Future Technologies ─────────────────────────────────────────
    ('[Far Future Technologies] Build an antimatter factory in Kerbin orbit',
     '[Far Future Technologies] Kerbin yörüngesinde antimadde fabrikası kurun',
     8, 'construction', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('[Far Future Technologies] Put an antimatter collector into Jool orbit',
     '[Far Future Technologies] Jool yörüngesine antimadde toplayıcı yerleştirin',
     8, 'construction', 'active_vessel', 'ORBITING', 'Jool'),
    ('[Far Future Technologies] Deploy a laser propulsion relay in Kerbin orbit',
     '[Far Future Technologies] Kerbin yörüngesine lazer itki rölesi yerleştirin',
     8, 'construction', 'active_vessel', 'ORBITING', 'Kerbin'),
    ('[Far Future Technologies] Construct a massive interstellar generation ship',
     '[Far Future Technologies] Devasa bir yıldızlararası nesil gemisi tasarlayın',
     9, 'construction', 'craft_build', None, None),

    # ── Mod: Near Future Technologies ────────────────────────────────────────
    ('[Near Future Technologies] Build a Mun base using Near Future Construction parts',
     '[Near Future Technologies] Near Future Construction parçalarıyla bir Mun üssü kurun',
     4, 'construction', 'active_vessel', 'LANDED', 'Mun'),
    ('[Near Future Technologies] Build a nuclear-powered orbital tug',
     '[Near Future Technologies] Nükleer güçle çalışan bir yörünge römorkörü tasarlayın',
     5, 'construction', 'craft_build', None, None),
    ('[Near Future Technologies] Reach Duna orbit using argon-fuelled ion propulsion',
     '[Near Future Technologies] Argon yakıtlı iyon itkisiyle Duna yörüngesine ulaşın',
     5, 'exploration', 'active_vessel', 'ORBITING', 'Duna'),
    ('[Near Future Technologies] Deploy a large solar array station in low Kerbol orbit',
     '[Near Future Technologies] Alçak Kerbol yörüngesine büyük bir güneş paneli istasyonu kurun',
     6, 'construction', 'active_vessel', 'ORBITING', 'Kerbol'),
    ('[Near Future Technologies] Reach Eeloo orbit using ion propulsion only',
     '[Near Future Technologies] Yalnızca iyon itkisiyle Eeloo yörüngesine ulaşın',
     6, 'exploration', 'active_vessel', 'ORBITING', 'Eeloo'),

    # ── Mod: Kerbalism / USI Life Support ────────────────────────────────────
    ('[Kerbalism / USI Life Support] Establish a USI MKS logistics hub in Mun orbit',
     '[Kerbalism / USI Life Support] Mun yörüngesinde USI MKS lojistik merkezi kurun',
     7, 'construction', 'active_vessel', 'ORBITING', 'Mun'),
    ('[Kerbalism / USI Life Support] Set up a resource extraction chain on Minmus',
     '[Kerbalism / USI Life Support] Minmus üzerinde bir kaynak çıkarma zinciri kurun',
     7, 'construction', 'active_vessel', 'LANDED', 'Minmus'),
    ('[Kerbalism / USI Life Support] Build a self-sufficient greenhouse base on Duna',
     "[Kerbalism / USI Life Support] Duna'da kendi kendine yeten bir sera üssü kurun",
     8, 'construction', 'active_vessel', 'LANDED', 'Duna'),

    # ── Mod: Real Solar System (RSS) / RO ────────────────────────────────────
    ('[Real Solar System] Reach Earth orbit',
     '[Real Solar System] Dünya yörüngesine ulaşın',
     5, 'orbital', 'active_vessel', 'ORBITING', 'Earth'),
    ('[Real Solar System] Perform a Moon landing',
     '[Real Solar System] Ay inişi yapın',
     7, 'landing', 'active_vessel', 'LANDED', 'Moon'),
    ('[Real Solar System] Put a probe into Jupiter orbit',
     "[Real Solar System] Jüpiter yörüngesine bir sonda yerleştirin",
     7, 'exploration', 'active_vessel', 'ORBITING', 'Jupiter'),
    ('[Real Solar System] Land a rover on Mars',
     "[Real Solar System] Mars'a bir gezici indirin",
     8, 'landing', 'active_vessel', 'LANDED', 'Mars'),
    ('[Real Solar System] Land on Venus',
     "[Real Solar System] Venüs'e iniş yapın",
     8, 'landing', 'active_vessel', 'LANDED', 'Venus'),
    ('[Real Solar System] Land a crew of three on the Moon',
     "[Real Solar System] Ay'a üç kişilik bir mürettebat indirin",
     8, 'return', 'active_vessel', 'LANDED', 'Moon'),
    ('[Real Solar System] Build the ISS in Earth orbit',
     "[Real Solar System] Dünya yörüngesinde ISS'i inşa edin",
     8, 'construction', 'active_vessel', 'ORBITING', 'Earth'),
    ('[Real Solar System] Send a Voyager-style probe out of the solar system',
     '[Real Solar System] Güneş sistemi dışına Voyager tarzı bir sonda gönderin',
     9, 'extreme', 'active_vessel', 'ESCAPING', 'Sun'),
    ('[Real Solar System] Perform a crewed Mars landing',
     '[Real Solar System] Mürettebatlı bir Mars inişi yapın',
     10, 'extreme', 'active_vessel', 'LANDED', 'Mars'),
]
