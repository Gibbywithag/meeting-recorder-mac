"""Build a deterministic meeting fixture with two voices and separate source tracks.
Run with a Python that has Pillow installed, then run gemini_pass.py on the output.
"""
import json
import subprocess
from pathlib import Path
from datetime import datetime
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = Path.home() / 'Meetings' / (datetime.now().strftime('%Y-%m-%d_%H%M%S') + '_client_simulation')
OUT.mkdir(parents=True)
font_path = '/System/Library/Fonts/Supplemental/Arial.ttf'
font = lambda n: ImageFont.truetype(font_path, n)
turns = [
 ('You', 0, 'Hi Jamie, this is Sam, your design consultant. I am recording our design review. Please show me the current homepage first.'),
 ('Client', 0, 'Hi Sam, I am Jamie, your client. I am sharing my screen now. This orange button on the right is too loud. Change the Book a demo button to navy blue, but keep the text white.'),
 ('You', 0, 'Understood. The button color should be navy blue and the text should stay white. What else on your homepage bothers you?'),
 ('Client', 0, 'This headline on the left is too small. Increase Welcome to Northstar to forty eight pixels. And remove the sale banner at the bottom. The twenty percent discount has expired.'),
 ('You', 1, 'I will make those three changes by Friday, October sixteenth. Now I am sharing my revised homepage. You are looking at my screen instead of yours.'),
 ('Client', 1, 'The navy button is better. Keep it. But this logo in the top left is too big now. Make the Northstar logo twenty percent smaller, and move the testimonial card below the pricing section.'),
 ('You', 1, 'Let me confirm. Keep the navy button, shrink the top left logo by twenty percent, and move the customer testimonial below pricing. Should we change the price too?'),
 ('Client', 1, 'No, keep the price at forty nine dollars per month. I will send you the approved logo file tomorrow, October fifteenth. Please send me the updated preview on Friday.'),
 ('You', 1, 'Agreed. Jamie sends the logo on October fifteenth. Sam sends the revised preview on October sixteenth. We are keeping the monthly price unchanged.'),
]

def run(args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout

def frame(stage):
    im = Image.new('RGB', (1280, 720), '#141c2d'); d = ImageDraw.Draw(im)
    d.rounded_rectangle((20,30,970,690), radius=14, fill='#f4f5f8')
    d.rectangle((20,30,970,85), fill='#354665')
    d.text((40,43), 'Jamie sharing: original homepage' if stage == 0 else 'Sam sharing: revised homepage', font=font(24), fill='white')
    d.text((50,105), 'NORTHSTAR', font=font(24 if stage == 0 else 40), fill='#183050')
    d.text((50,200), 'Welcome to Northstar', font=font(28 if stage == 0 else 48), fill='#183050')
    d.text((50,280), 'Simple tools for your growing business', font=font(22), fill='#556070')
    d.rounded_rectangle((650,310,930,375),radius=10,fill='#f97316' if stage == 0 else '#183050')
    d.text((690,328),'Book a demo',font=font(28),fill='white')
    d.rounded_rectangle((50,405,930,485),radius=10,fill='#d9e5f0')
    d.text((70,430),'Testimonial: Our customers love Northstar',font=font(25),fill='#183050')
    d.text((50,520),'Pricing: $49 / month',font=font(30),fill='#183050')
    if stage == 0:
        d.rectangle((50,590,930,660),fill='#ffd2a5'); d.text((70,610),'SALE: 20% OFF',font=font(32),fill='#702a00')
    d.rounded_rectangle((990,30,1260,690),radius=14,fill='#283449')
    d.text((1010,60),'SIMULATED CALL',font=font(21),fill='white')
    for y,name,color in [(130,'Jamie - Client','#a5cafa'),(360,'Sam - Consultant','#88d6b0')]:
        d.ellipse((1035,y,1215,y+180),fill=color)
        d.text((1010,y+205),name,font=font(20),fill='white')
    path=OUT/f'screen_{stage}.png'; im.save(path); return path
screens = [frame(0),frame(1)]
manifest=[]; offset=0
for i,(speaker,stage,text) in enumerate(turns):
    audio=OUT/f'voice_{i}.aiff'
    run(['say','-v','Daniel' if speaker=='You' else 'Samantha','-r','175','-o',str(audio),text])
    duration=float(run(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(audio)]).strip())+0.8
    local,remote=('1:a','2:a') if speaker=='You' else ('2:a','1:a')
    run(['ffmpeg','-y','-v','error','-loop','1','-framerate','5','-i',str(screens[stage]),'-i',str(audio),'-f','lavfi','-i','anullsrc=r=48000:cl=stereo','-map','0:v','-map',local,'-map',remote,'-af','apad','-t',str(duration),'-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-ar','48000','-ac','2',str(OUT/f'turn_{i}.mov')])
    manifest.append({'t':offset,'speaker':speaker,'presenter':'Jamie' if stage==0 else 'Sam','text':text}); offset+=duration
(OUT/'concat.txt').write_text(''.join(f"file 'turn_{i}.mov'\n" for i in range(len(turns))))
run(['ffmpeg','-y','-v','error','-f','concat','-safe','0','-i',str(OUT/'concat.txt'),'-map','0','-c','copy',str(OUT/'meeting.mov')])
(OUT/'expected.json').write_text(json.dumps(manifest,indent=2))
(OUT/'scenario.md').write_text('# Simulated client design review\n\n'+ '\n\n'.join(f"{x['t']:.1f}s — {x['speaker']} ({x['presenter']} presenting): {x['text']}" for x in manifest))
print(OUT,flush=True)
