"""Two deterministic arithmetic items demonstrating the suite interface."""
import argparse
import json
from pathlib import Path
from research_sdk import Client

p=argparse.ArgumentParser();p.add_argument('--context',required=True);p.add_argument('--output',required=True);args=p.parse_args()
client=Client(args.context);items=[]
for ident,prompt,answer in [('sum','What is 2 + 2? Reply with the numeral only.','4'),('product','What is 2 * 3? Reply with the numeral only.','6')]:
    items.append(client.item(ident,prompt,lambda text,answer=answer:float(text.strip()==answer)))
    target=Path(args.output);temp=target.with_name(target.name+'.tmp')
    temp.write_text(json.dumps({'protocol_version':1,'items':items}));temp.replace(target)
