"""The writer's whole prompt goes out verbatim plus only the allowed additions (owner 2026-09-26
"最关键的一步，必须100%做到").

The prompts below are real Higgsfield ledger prompts (HOL-RO study only, private repo), with the stored
`<<<element-uuid>>>` tokens turned back into the `@name` the writer typed: cully `9b13e217` (seedance 2.5, a full
CINEDANCE prompt), the cully:90 brief example's REFERENCES line, oneiric `e668a8a7` with element `12802b4e`
(REFERENCE DEFINITIONS already adapted per shot), trigger `06c35f81` (CHARACTERS line, GRADE look), hell-grind
`6356e689` (SUBJECT LOCK `[img_roco_cold]`) and zephyr-special `2411572b` (COLOUR / LIGHT look, music video).
"""
import unittest

from production import prompt

CULLY_9B13E217 = '''SCENE CONTEXT
A two-part duel insert in one shot: 5 seconds on the llama giving Rodger a strange, unsettling SMILE, then a HARD CUT to 5 seconds reverse on Rodger — STANDING upright facing the llama — staring back, his own friendly half-smile fading into suspicion. Both halves are MATCHED singles of the SAME size.

ACTIVE REFERENCES
@loc_CB_stort_bridge_s58-59 — a stacked-stone arched road bridge over the narrow River Stort near Harlow, a single-track lane crossing it, flat golden-brown winter farmland with post-and-rail fences and reeds along the water, cool overcast daylight; controls geography, materials, the bridge, water and the soft daylight; not camera angle or story identity. Swap for the real asset.
@char_CB_Roger_v3_3_noleafs — 40s ex-con, wild dark curly hair greying at the temples, full grey-streaked beard, a scar on one cheek, navy torn boiler-suit over a grubby white tee, bullet-wounded calf; his tired half-smile fades into wary, dawning suspicion. 100% matches the reference.
@lama_1 — the large white-and-light-brown fluffy llama, dead-centre on the bridge, giving Rodger a strange, unsettling SMILE — its lips curling back into a weird, toothy llama grin while its eyes stay flat. 100% matches the reference.

LOCATION MAP
On the stone bridge: @lama_1 stands dead-centre facing Rodger off screen-RIGHT (its eyeline); @char_CB_Roger_v3_3_noleafs stands at the near-centre facing the llama off screen-LEFT.

ACTION TIMING
0.0–2.5s — CU (47°, 50mm). Camera at the llama's head height on the bridge, ~1.5 m, locked dead still — a clean tight single of @lama_1's head filling the frame.
5.0s — HARD CUT to the reverse.
5.0–7.5s — matched CU (47°, 50mm) reverse single on @char_CB_Roger_v3_3_noleafs, STANDING upright facing the off-frame llama.

AUDIO
Wind over the field, the slow river; on the reverse a small uneasy exhale from Rodger; no music, no line.

STYLE
Grounded photoreal British social-realist crime-comedy: damp overcast rural Harlow, lived-in surfaces, deadpan menace under absurd comedy; the violence and threat played plainly, never glamorous.

QUALITY
ARRI Alexa, vintage spherical primes, subtle anamorphic; sharp clarity, stable picture, natural colours, readable faces, controlled highlights, no grain, no modern tech beyond 2011, British spelling.

POSITIVE CONSTRAINTS
EXACTLY ONE llama (@lama_1) anywhere in frame — only a single llama exists; never two. Lens 50mm. British spelling.'''

# cully-hill-boys brief line 90: the whole prompt as it went to the model (no STYLE block; POSITIVE LOCKS closes it).
CULLY_90 = '''OPTICS: 200mm long telephoto (FOV ≈12°) — heavy compression, SHALLOW depth of field; a tight CLOSE-UP two-shot of the two lads' faces, corridor compressed and soft behind.
REFERENCES
@loc_CB_commons_backstage (THE COMMONS — BACKSTAGE CORRIDOR, night): cool-white fluorescent box-fixtures, white-painted brick + red brick walls, a WHITE DOOR (SHUT the entire take). Controls geometry, materials, light and atmosphere ONLY — not framing.
@char_CB_Kel (CAL — beaten, frame-LEFT): late-20s, thin/exhausted, messy dark hair, stubble, FACE scrapes + scalp HEAD WOUND, BLOODIED dirty white tee. 100% matches.
FORMAT — ~12–14 seconds, real-time, ONE continuous shot, no cuts, no fades. NO music score — muffled gig bleed only.
0.0s–3.0s — 200mm tight two-shot: CAL (frame-LEFT, the taller head) watches OLI (OFF-SCREEN behind the camera).
POSITIVE LOCKS
EYELINE LOCK: both eyelines sit just BESIDE the lens — NEVER into it. Colour 60:30:10. Nothing modern beyond 2011.'''

ONEIRIC_12802B4E = '''— REFERENCE DEFINITIONS —

@char_ON_Sam_s2_v1: slim 18-year-old man, short (~170 cm), ginger hair, rimless glasses, navy-and-cream raglan "Quantum" tee, light-blue jeans; a floppy slice of pizza in his hand; deep inside the story he is telling, chin-up and proud. Character appearance only. Reference.

@loc_ON_dorm_commonroom_front_s2: warm cluttered student common room — beige couch, brown plush armchair, beige armchair, cluttered coffee table, sci-fi posters, window light, cozy amber shade. Location and mood reference. Reference.

— TECHNICAL BLOCK —

Ultra filmic, photoreal live-action. 16:9. 16s. Dialogue + SFX, no music, no subtitles.

— PROMPT —

Open on a static, locked-off, silent establishing wide: @char_ON_Sam_s2_v1 alone in the warm cluttered @loc_ON_dorm_commonroom_front_s2, seated in the beige armchair on screen-right — his usual spot.'''

TRIGGER_06C35F81 = '''Duration: 12s. THIS IS A NIGHT SCENE — 2 A.M., THE HARDEST RULE, IT OVERRIDES EVERYTHING.
GRADE: Fight Club night grade, exactly David Fincher and DP Jeff Cronenweth — dirty sodium greenish-yellow highlights, grimy green-teal shadows, crushed murky blacks with no lift, low saturation, pale greenish skin, fine film grain.
CHARACTERS: JACK @char_SN_Jack — lean man ~45 in a black knitted wool balaclava with THREE holes (two eyes, one mouth), black hooded windbreaker, black gloves, the rifle case  slung tight on his back on two straps. TERRENCE  — his target, standing in the yard with a black pistol.
0-4s | THE STANDOFF, HELD. Yard almost entirely black. Handheld, low. JACK stands where he stopped, two steps down into the yard, the case on his back, masked.
SOUND: distant city hum, two men breathing in a closed yard — then ONE hard pistol report into the lens, and dead silence over black. No music.'''

HELL_GRIND_SUBJECT_LOCK = '''OUTPUT: 8 seconds, 21:9. Two timed segments inside one continuous video.

SUBJECT LOCK:
@ROCO_COLD [img_roco_cold] — 20yo lean, cropped dark messy hair, torn dark t-shirt with anatomical skin/muscle panels, dark cargo pants.
@JAX_LATE [img_jax_late] — 20yo dark-skinned, very short bleached-blond buzz cut, three dark forest-green war-paint stripes across nose and cheekbones. Voice raw, cracking.

WORLD POSITIONS — character @ROCO_COLD stands at the lone tree; @JAX_LATE faces him.'''

ZEPHYR_SPECIAL_2411572B = '''Style: photoreal cinematic music-video frame, real film grain. NOT a 3D render, NOT a game engine, NOT a cartoon. Vivid, punchy MV look.

FORMAT: anamorphic, 21:9, ~6s, real-time.

SHOT: dynamic CLOSE-UPS of @Sheet_naomi (NAOMI) playing the @drums_naomi drum kit - tight on her face and arms as she drums.

COLOUR / LIGHT: bright, vivid and saturated, airy daylight from @water_loca; backlight catching the flying water; clean believable skin.

AUDIO: drum hits in sync with each strike; performance locked to the input music track. (Music from input.)'''

LOOK = 'Photoreal live-action, ARRI Alexa, soft contrast, natural grain.'


def asset(descriptor=None, image=None):
    return {'descriptor': descriptor, 'image': image}


class BuildTests(unittest.TestCase):
    def build(self, text, assets, looks=None, look_name=None, seconds=12, binding='every'):
        sent, images, additions = prompt.build(text, assets, looks or {}, look_name, seconds, binding)
        self.assertEqual(prompt.apply(text, additions), sent)
        constants = prompt.constants(assets, looks or {}, seconds)
        self.assertEqual(prompt.check(text, additions, sent, images, constants), [])
        return sent, images, additions

    def kinds(self, additions):
        return [a['kind'] for a in additions]

    def test_full_cinedance_prompt_only_gets_image_binds(self):
        assets = {'loc_CB_stort_bridge_s58-59': asset('stone bridge', 'img-bridge'),
                  'char_CB_Roger_v3_3_noleafs': asset('Rodger, 40s ex-con', 'img-roger'),
                  'lama_1': asset('a white llama', 'img-llama')}
        sent, images, additions = self.build(CULLY_9B13E217, assets, {'world': LOOK})
        self.assertEqual([(i['n'], i['tag'], i['image']) for i in images],
                         [(1, 'loc_CB_stort_bridge_s58-59', 'img-bridge'), (2, 'char_CB_Roger_v3_3_noleafs', 'img-roger'),
                          (3, 'lama_1', 'img-llama')])
        self.assertEqual(set(self.kinds(additions)), {'bind'})
        self.assertEqual(sent.count('@Image3 @lama_1'), CULLY_9B13E217.count('@lama_1'))
        self.assertEqual(sent.replace('@Image1 ', '').replace('@Image2 ', '').replace('@Image3 ', ''), CULLY_9B13E217)
        self.assertNotIn(LOOK, sent)  # the writer wrote a STYLE block

    def test_a_video_reference_is_numbered_apart_as_video(self):
        """(passport-rush: "<<<video_1>>> — motion, camera and timing reference ONLY"): a tag whose
        asset is a video is bound @VideoK, numbered apart from the images; check proves the send both ways."""
        text = ('ACTIVE REFERENCES\n@girl — the heroine, 100% matches the reference\n@taxi_previs — motion, camera and timing '
                'reference only; the grey-box look is not inherited\n\nACTION TIMING\n0-4s @girl rides in the taxi as '
                '@taxi_previs moves; @girl leans out.\n\n4s. No music.')
        assets = {'girl': asset('a girl in a yellow raincoat', 'img-girl'),
                  'taxi_previs': {'descriptor': None, 'image': None, 'video': 'vid-taxi'}}
        for binding in ('every', 'first'):
            with self.subTest(binding=binding):
                sent, images, additions = self.build(text, assets, binding=binding)
                self.assertEqual([(i['n'], i['tag'], i['media']) for i in images], [(1, 'girl', 'image'), (1, 'taxi_previs', 'video')])
                self.assertIn('@Video1 @taxi_previs — motion', sent)
                self.assertIn('@Image1 @girl — the heroine', sent)
                self.assertNotIn('@Image2', sent)
                constants = prompt.constants(assets, {}, 12)
                # The video number on an image slot, or the other way round, is refused.
                swapped = [dict(a, text='@Image1 ') if a.get('media') == 'video' else a for a in additions]
                self.assertTrue(prompt.check(text, swapped, prompt.apply(text, swapped), images, constants))
                as_images = [{k: v for k, v in i.items() if k != 'media'} for i in images]
                self.assertTrue(prompt.check(text, additions, sent, as_images, constants))

    def test_the_higgsfield_style_numbers_references_hfs_way(self):
        """on Higgsfield the number is HF's own token (<<<image_1>>>, <<<video_1>>>); check holds
        the style frozen in the constants, so an @Image1 on a Higgsfield prompt is refused and the other way round."""
        text = '@girl — the heroine\n@taxi_previs — motion reference only\n\n0-4s @girl rides.\n\n4s. No music.'
        assets = {'girl': asset('a girl in a yellow raincoat', 'img-girl'),
                  'taxi_previs': {'descriptor': None, 'image': None, 'video': 'vid-taxi'}}
        sent, images, additions = prompt.build(text, assets, {}, None, 4, 'first', 'hf')
        self.assertIn('<<<image_1>>> @girl — ', sent)
        self.assertIn('<<<video_1>>> @taxi_previs — motion', sent)
        self.assertNotIn('@Image', sent)
        hf = prompt.constants(assets, {}, 4, 'hf')
        self.assertEqual(prompt.check(text, additions, sent, images, hf), [])
        self.assertTrue(prompt.check(text, additions, sent, images, prompt.constants(assets, {}, 4)))  # read as fal: refused
        at_sent, at_images, at_additions = prompt.build(text, assets, {}, None, 4, 'first')
        self.assertTrue(at_sent.startswith('@Image1 @girl'))
        self.assertTrue(prompt.check(text, at_additions, at_sent, at_images, hf))
        self.assertNotIn('reference_style', prompt.constants(assets, {}, 4))  # fal constants keep their earlier shape

    def test_cully_90_references_line_with_parenthetical_is_already_described(self):
        assets = {'loc_CB_commons_backstage': asset('THE COMMONS corridor constant', 'a'),
                  'char_CB_Kel': asset('Kel constant descriptor', 'b')}
        sent, _images, additions = self.build(CULLY_90, assets, {'world': LOOK})
        self.assertNotIn('constant', sent)
        self.assertNotIn('No music', sent.replace('NO music score', ''))  # "music score" is a mention
        self.assertNotIn('12s.', sent)  # "~12–14 seconds" is a stated duration
        # No STYLE/LOOK/GRADE/COLOUR section: the world look goes in as a STYLE block before POSITIVE LOCKS.
        self.assertIn(f'STYLE: {LOOK}\n\nPOSITIVE LOCKS', sent)
        self.assertEqual(self.kinds(additions), ['bind', 'bind', 'look'])

    def test_oneiric_adapted_descriptor_is_never_replaced_by_the_registry(self):
        assets = {'char_ON_Sam_s2_v1': asset('slim 18-year-old man, ginger hair, rimless glasses', 'sam'),
                  'loc_ON_dorm_commonroom_front_s2': asset('student common room', 'room')}
        sent, _images, additions = self.build(ONEIRIC_12802B4E, assets)
        self.assertEqual(self.kinds(additions), ['bind'] * 4)
        self.assertIn('@Image1 @char_ON_Sam_s2_v1: slim 18-year-old man, short (~170 cm)', sent)
        self.assertIn('wide: @Image1 @char_ON_Sam_s2_v1 alone in the warm cluttered @Image2 @loc_ON_dorm_commonroom_front_s2,', sent)

    def test_trigger_characters_line_and_grade_look_paste_nothing(self):
        assets = {'char_SN_Jack': asset('Jack constant descriptor', 'jack')}
        sent, _images, additions = self.build(TRIGGER_06C35F81, assets, {'city': LOOK})
        self.assertEqual(self.kinds(additions), ['bind'])
        self.assertIn('CHARACTERS: JACK @Image1 @char_SN_Jack — lean man ~45', sent)
        self.assertNotIn(LOOK, sent)

    def test_a_tag_only_in_short_lines_gets_its_descriptor_in_the_characters_block(self):
        text = TRIGGER_06C35F81 + '\n@char_SN_Terrence waits.'
        assets = {'char_SN_Jack': asset('Jack constant', 'jack'),
                  'char_SN_Terrence': asset('man ~45, black jacket, black pistol', 'terrence')}
        sent, _images, additions = self.build(text, assets)
        self.assertIn('two straps. TERRENCE  — his target, standing in the yard with a black pistol.\n'
                      '@char_SN_Terrence — man ~45, black jacket, black pistol\n0-4s | THE STANDOFF', sent)
        self.assertTrue(sent.endswith('No music.\n@Image2 @char_SN_Terrence waits.'))
        self.assertEqual(self.kinds(additions), ['bind', 'descriptor', 'bind'])

    def test_hell_grind_subject_lock_line_is_already_described(self):
        assets = {'ROCO_COLD': asset('Roco constant', 'roco'), 'JAX_LATE': asset('Jax constant', 'jax')}
        sent, _images, additions = self.build(HELL_GRIND_SUBJECT_LOCK, assets, binding='first')
        self.assertIn('@Image1 @ROCO_COLD [img_roco_cold] — 20yo lean', sent)
        self.assertIn('character @ROCO_COLD stands at the lone tree; @JAX_LATE faces him.', sent)  # first mention only
        self.assertEqual(self.kinds(additions), ['bind', 'bind', 'no_music'])

    def test_zephyr_special_colour_light_look_and_music_video(self):
        assets = {'Sheet_naomi': asset('Naomi constant', 'n'), 'drums_naomi': asset(None, 'd'), 'water_loca': asset(None, 'w')}
        text = ZEPHYR_SPECIAL_2411572B.split('\n\n', 1)[1]  # without the "Style:" line: COLOUR / LIGHT alone is a look
        sent, images, additions = self.build(text, assets, {'world': LOOK}, seconds=6)
        self.assertEqual(self.kinds(additions), ['bind', 'bind', 'bind'])
        self.assertEqual([i['tag'] for i in images], ['Sheet_naomi', 'drums_naomi', 'water_loca'])
        sent, _, additions = self.build(ZEPHYR_SPECIAL_2411572B, assets, {'world': LOOK}, seconds=6)
        self.assertNotIn(LOOK, sent)  # "Style:" prefix line
        self.assertNotIn('No music.', sent)  # "music-video" is a mention of music

    def test_bare_tag_line_gets_the_descriptor_after_its_separator(self):
        text = 'ACTIVE REFERENCES\n@kel (CAL):\n@room\n\nACTION\n@kel sits.\n\n8s. No music.'
        assets = {'kel': asset('late-20s, thin, messy dark hair', 'k'), 'room': asset('a small kitchen at night', 'r')}
        sent, _, _ = self.build(text, assets)
        self.assertEqual(sent, 'ACTIVE REFERENCES\n@Image1 @kel (CAL): late-20s, thin, messy dark hair\n'
                               '@Image2 @room — a small kitchen at night\n\nACTION\n@Image1 @kel sits.\n\n8s. No music.')

    def test_no_references_block_gets_a_new_active_references_block_at_the_top(self):
        text = 'SHOT: @kel waits.\nAUDIO: rain. 10s. No music.'
        sent, _, _additions = self.build(text, {'kel': asset('late-20s, thin, messy dark hair', 'k')})
        self.assertEqual(sent, 'ACTIVE REFERENCES:\n@kel — late-20s, thin, messy dark hair\n\n'
                               'SHOT: @Image1 @kel waits.\nAUDIO: rain. 10s. No music.')

    def test_chinese_prompt_stated_seconds_and_no_music_line(self):
        text = '镜头：@kel 推门走进厨房，停在桌边，回头看门口，慢慢坐下。时长 12 秒。\n声音：雨声，冰箱嗡嗡响。'
        sent, _images, additions = self.build(text, {'kel': asset('late-20s, thin', 'k')})
        self.assertEqual(sent, '镜头：@Image1 @kel 推门走进厨房，停在桌边，回头看门口，慢慢坐下。时长 12 秒。\n声音：雨声，冰箱嗡嗡响。\n\nNo music.')
        self.assertEqual(self.kinds(additions), ['bind', 'no_music'])

    def test_ages_written_as_decades_are_not_durations(self):
        for text in ('@kel — late-20s, thin, messy dark hair.', '@kel — 40s ex-con, wild dark curly hair.',
                     '@kel, a man in his 30s, waits by the door.'):
            _, _, additions = self.build(text, {'kel': asset(None, 'k')}, seconds=9)
            self.assertIn('duration', self.kinds(additions), text)
        _, _, additions = self.build('Duration: 20s. @kel waits by the door.', {'kel': asset(None, 'k')}, seconds=20)
        self.assertNotIn('duration', self.kinds(additions))

    def test_two_worlds_paste_the_named_look_and_zero_images(self):
        looks = {'city': 'CITY LOOK: sodium night.', 'dream': 'Soft pastel dream, hazy bloom.'}
        text = 'A wide empty street at night, rain on the asphalt.'
        sent, images, additions = self.build(text, {}, looks, 'dream', seconds=5)
        self.assertEqual(images, [])
        self.assertEqual(sent, text + '\n\nSTYLE: Soft pastel dream, hazy bloom.\n\n5s. No music.')
        sent, _, additions = self.build(text, {}, looks, None, seconds=5)  # two looks, none named: nothing to paste
        self.assertEqual(self.kinds(additions), ['duration', 'no_music'])
        with self.assertRaises(ValueError):
            prompt.build(text, {}, looks, 'noir', 5, 'every')

    def test_writer_bytes_are_never_changed(self):
        for text in (CULLY_9B13E217, CULLY_90, ONEIRIC_12802B4E, TRIGGER_06C35F81, HELL_GRIND_SUBJECT_LOCK, ZEPHYR_SPECIAL_2411572B):
            assets = {tag: asset('some constant descriptor words', tag) for tag in prompt.tags(text)}
            _sent, _, additions = prompt.build(text, assets, {'w': LOOK}, None, 9, 'every')
            rebuilt, at = [], 0
            for a in additions:
                rebuilt.append(text[at:a['at']])
                at = a['at']
            rebuilt.append(text[at:])
            self.assertEqual(''.join(rebuilt), text)


    # Bug hunt 2026-09-27.
    def test_written_seconds_decades_and_music_words(self):
        for text in ('A 5-second shot: @kel waits.', '@kel waits for 1 second, then turns.'):
            _, _, additions = self.build(text, {'kel': asset(None, 'k')}, seconds=6)
            self.assertNotIn('duration', self.kinds(additions), text)  # the writer stated one; nothing contradicts it
        _, _, additions = self.build('A 1980s kitchen. @kel waits.', {'kel': asset(None, 'k')}, seconds=6)
        self.assertIn('duration', self.kinds(additions))  # a decade is not a duration
        _, _, additions = self.build('@kel looks up at the scoreboard.', {'kel': asset(None, 'k')}, seconds=6)
        self.assertIn('no_music', self.kinds(additions))  # scoreboard is not music

    def test_the_writers_own_look_labels_and_a_slug_that_is_not_one(self):
        for text in ('@kel waits.\n\nLook: cold neon, deep blacks.', '@kel waits.\n\nGrade: bleach bypass.',
                     '@kel waits.\n\nColour: teal and orange.', '@kel waits.\r\n\r\nSTYLE\r\nfilm grain.'):
            _, _, additions = self.build(text, {'kel': asset(None, 'k')}, {'w': LOOK})
            self.assertNotIn('look', self.kinds(additions), repr(text))
        _, _, additions = self.build('EXT. GRADE SCHOOL — DAY\n@kel waits.', {'kel': asset(None, 'k')}, {'w': LOOK})
        self.assertIn('look', self.kinds(additions))

    def test_an_inline_descriptor_never_lands_after_the_tail(self):
        sent, _, _ = self.build('SHOT: @kel waits.\n@kel:  ', {'kel': asset('Kel, courier.', 'k')}, seconds=6)
        self.assertTrue(sent.rstrip().endswith('6s. No music.'), sent)
        self.assertIn('@kel: Kel, courier.', sent.replace('@Image1 ', ''))


    def test_with_first_binding_an_added_line_before_the_writer_carries_the_number(self):
        # Re-audit 2026-09-27: the owner's "number on the first mention" holds for what is sent.
        assets = {'kel': asset('late-20s courier', 'k'), 'room': asset('a small kitchen', 'r')}
        sent, _, additions = self.build('@kel enters @room.\nClose on @kel.', assets, seconds=6, binding='first')
        self.assertEqual(sent, 'ACTIVE REFERENCES:\n@Image1 @kel — late-20s courier\n@Image2 @room — a small kitchen\n\n'
                               '@kel enters @room.\nClose on @kel.\n\n6s. No music.')
        self.assertNotIn('bind', self.kinds(additions))
        text = 'ACTIVE REFERENCES\n@room\n\nSHOT: @kel enters @room.'  # a line added at the end of the writer's block
        sent, _, _ = self.build(text, assets, seconds=6, binding='first')
        self.assertTrue(sent.startswith('ACTIVE REFERENCES\n@Image1 @room — a small kitchen\n@Image2 @kel — late-20s courier'), sent)
        self.assertIn('SHOT: @kel enters @room.', sent)


class CheckTests(unittest.TestCase):
    def setUp(self):
        self.text = 'ACTIVE REFERENCES\n@kel:\n\nSHOT: @kel waits by @room.'
        self.assets = {'kel': asset('late-20s, thin, messy dark hair', 'k'), 'room': asset('a small kitchen', 'r')}
        self.sent, self.images, self.additions = prompt.build(self.text, self.assets, {'w': LOOK}, None, 8, 'every')
        self.constants = prompt.constants(self.assets, {'w': LOOK}, 8)

    def test_clean(self):
        self.assertEqual(prompt.check(self.text, self.additions, self.sent, self.images, self.constants), [])

    def test_a_changed_byte_in_what_was_sent_fails(self):
        problems = prompt.check(self.text, self.additions, self.sent.replace('waits', 'runs'), self.images, self.constants)
        self.assertTrue(any('writer text plus the additions' in p for p in problems))

    def test_an_addition_outside_its_allowed_set_fails(self):
        for kind, text in (('look', 'STYLE: something the platform made up\n\n'), ('no_music', ' No music at all.'),
                           ('duration', ' 9s.'), ('descriptor', ' a descriptor nobody registered')):
            forged = [*self.additions, {'kind': kind, 'at': len(self.text), 'text': text}]
            sent = prompt.apply(self.text, forged)
            self.assertTrue(prompt.check(self.text, forged, sent, self.images, self.constants), kind)

    def test_tail_additions_come_once_and_only_when_build_would_add_them(self):
        for extra in ({'kind': 'no_music', 'text': ' No music.'}, {'kind': 'duration', 'text': '\n\n8s.'}):
            forged = [*self.additions, {**extra, 'at': len(self.text)}]
            forged.sort(key=lambda a: (a['at'], prompt.ORDER[a['kind']]))
            self.assertTrue(prompt.check(self.text, forged, prompt.apply(self.text, forged), self.images, self.constants), extra)
        text = self.text + '\n10s. Score: none.'
        _, images, additions = prompt.build(text, self.assets, {'w': LOOK}, None, 8, 'every')
        forged = [*additions, {'kind': 'no_music', 'at': len(text), 'text': '\n\nNo music.'},
                  {'kind': 'duration', 'at': len(text), 'text': '\n\n8s.'}]
        forged.sort(key=lambda a: (a['at'], prompt.ORDER[a['kind']]))
        problems = prompt.check(text, forged, prompt.apply(text, forged), images, self.constants)
        self.assertTrue(any('writer stated one' in p for p in problems) and any('about music' in p for p in problems), problems)

    def test_a_descriptor_only_where_the_writer_did_not_describe_and_once(self):
        described = 'ACTIVE REFERENCES\n@kel: a courier in a yellow coat, soaked\n\nSHOT: @kel waits by @room.'
        _, images, additions = prompt.build(described, self.assets, {'w': LOOK}, None, 8, 'every')
        forged = sorted([*additions, {'kind': 'descriptor', 'at': len('ACTIVE REFERENCES\n@kel: a courier in a yellow coat, soaked'),
                                      'text': ' — late-20s, thin, messy dark hair', 'tag': 'kel'}], key=lambda a: a['at'])
        problems = prompt.check(described, forged, prompt.apply(described, forged), images, self.constants)
        self.assertTrue(any('which the writer described' in p for p in problems), problems)
        twice = sorted([*self.additions, {'kind': 'descriptor', 'at': len(self.text), 'text': '\n@room — a small kitchen', 'tag': 'room'}],
                       key=lambda a: a['at'])
        problems = prompt.check(self.text, twice, prompt.apply(self.text, twice), self.images, self.constants)
        self.assertTrue(any('more than one descriptor for @room' in p for p in problems), problems)

    def test_a_bind_must_sit_on_its_own_tag_with_its_own_number(self):
        moved = [dict(a) for a in self.additions]
        bind = next(a for a in moved if a['kind'] == 'bind' and a['tag'] == 'room')
        bind['text'] = '@Image1 '
        problems = prompt.check(self.text, moved, prompt.apply(self.text, moved), self.images, self.constants)
        self.assertTrue(any('@Image' in p for p in problems))

    def test_image_numbers_must_run_one_to_n(self):
        images = [dict(i) for i in self.images]
        images[1]['n'] = 3
        self.assertTrue(prompt.check(self.text, self.additions, self.sent, images, self.constants))

    def test_additions_may_not_overlap_or_split_a_tag(self):
        split = [*self.additions, {'kind': 'no_music', 'at': self.text.index('@room') + 2, 'text': 'No music.'}]
        split.sort(key=lambda a: a['at'])
        self.assertTrue(prompt.check(self.text, split, prompt.apply(self.text, split), self.images, self.constants))


class AdviceTests(unittest.TestCase):
    """HF practice the writer is reminded of; advice only, never a refusal (HF_CANONICAL.md §5-6)."""

    def advice(self, text, assets=None, seconds=12):
        return prompt.advice(text, assets or {}, seconds)

    def test_full_hf_prompts_get_no_lens_or_beat_advice(self):
        for text in (CULLY_9B13E217, CULLY_90):
            notes = self.advice(text, {t: asset(None, t) for t in prompt.tags(text)}, seconds=10 if text is CULLY_9B13E217 else 12)
            self.assertFalse([n for n in notes if 'lens' in n or 'timed beats' in n], notes)

    def test_no_lens_and_no_timed_beats(self):
        notes = self.advice('A wide empty street at night, rain on the asphalt. 12s.')
        self.assertTrue(any('lens' in n and '9 of 12' in n for n in notes), notes)
        self.assertTrue(any('timed beats' in n and '6 of 12' in n for n in notes), notes)
        self.assertFalse(self.advice('50mm. 0-6s | he waits. 6-12s | he leaves.'))
        self.assertFalse(self.advice('47° lens. 0.0–2.5s — he waits; 2.5–12.0s — he leaves.'))
        self.assertFalse(self.advice('35毫米镜头。0-6秒：他等着。6-12秒：他离开。'))

    def test_stated_duration_differs_from_the_card(self):
        notes = self.advice(ONEIRIC_12802B4E, {t: asset(None, t) for t in prompt.tags(ONEIRIC_12802B4E)}, seconds=15)
        self.assertTrue(any('16' in n and '15' in n and 'duration' in n for n in notes), notes)
        self.assertFalse([n for n in self.advice(CULLY_90, {t: asset(None, t) for t in prompt.tags(CULLY_90)}, seconds=13)
                          if 'duration' in n])  # "~12–14 seconds" covers 13
        self.assertFalse([n for n in self.advice(TRIGGER_06C35F81, {'char_SN_Jack': asset()}) if 'duration' in n])
        self.assertTrue([n for n in self.advice(TRIGGER_06C35F81, {'char_SN_Jack': asset()}, seconds=15) if 'duration' in n])

    def test_unknown_at_word(self):
        notes = self.advice('50mm. 0-12s | @kel waits for @Image1 and @nobody.', {'kel': asset('late-20s', 'k')})
        self.assertTrue(any('@nobody' in n for n in notes), notes)
        self.assertTrue(any('@Image1' in n for n in notes), notes)
        self.assertFalse(any('@kel' in n for n in notes), notes)

    def test_registry_descriptor_and_writer_line_disagree_on_a_state_word(self):
        text = '50mm. ACTIVE REFERENCES\n@kel — late-20s, thin, messy dark hair, dry white tee.\n0-12s | @kel waits.'
        notes = self.advice(text, {'kel': asset('late-20s, thin, soaked wet white tee, dripping hair', 'k')})
        self.assertTrue(any('@kel' in n and 'wet' in n for n in notes), notes)
        same = self.advice(text.replace('dry', 'wet'), {'kel': asset('late-20s, wet white tee', 'k')})
        self.assertFalse([n for n in same if '@kel' in n], same)
        self.assertFalse(self.advice('50mm. 0-12s | @kel waits.', {'kel': asset('soaked wet tee', 'k')}))  # no writer line


if __name__ == '__main__':
    unittest.main()
