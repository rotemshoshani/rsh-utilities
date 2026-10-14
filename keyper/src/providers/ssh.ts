import { KeyperError } from "../errors.js";
import { runSafeProcess } from "../safe-process.js";
import type { InventoryEntry, KeyProvider, SshAlias, WriteOptions } from "../types.js";

export const REMOTE_SCRIPT = String.raw`
import base64,json,os,re,stat,sys,tempfile

meta=json.loads(base64.urlsafe_b64decode(sys.argv[1]+'==='))
path,op,name=meta['path'],meta['op'],meta.get('name')
pattern=re.compile(r'^\s*(?:export\s+)?([A-Za-z][A-Za-z0-9_]*)\s*=\s*(.*)$')

def load():
    try:
        st=os.lstat(path)
        if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode): raise RuntimeError()
        with open(path,'r',encoding='utf-8',newline='') as handle: return handle.read()
    except FileNotFoundError:
        return ''

def closed(text,quote):
    slashes=0
    for char in text:
        if char==quote and slashes%2==0: return True
        slashes=slashes+1 if char=='\\' else 0
    return False

def records(content):
    lines=content.splitlines()
    found=[]
    index=0
    while index<len(lines):
        match=pattern.match(lines[index])
        if not match:
            index+=1
            continue
        end=index
        rest=match.group(2)
        quote=rest[0] if rest[:1] in ('\"',"'",chr(96)) else None
        joined=rest[1:] if quote else rest
        while quote and not closed(joined,quote) and end+1<len(lines):
            end+=1
            joined+='\n'+lines[end]
        if quote and not closed(joined,quote): raise RuntimeError()
        found.append((match.group(1),index,end,rest))
        index=end+1
    names=[item[0] for item in found]
    if len(names)!=len(set(names)): raise RuntimeError()
    return lines,found

def decode(rest,lines,start,end):
    raw='\n'.join([rest]+lines[start+1:end+1])
    if raw[:1] not in ('\"',"'",chr(96)):
        return raw.split('#',1)[0].strip() if meta['format']=='dotenv' else raw.strip()
    quote=raw[0]
    body=raw[1:]
    position=0
    slashes=0
    while position<len(body):
        char=body[position]
        if char==quote and slashes%2==0: break
        slashes=slashes+1 if char=='\\' else 0
        position+=1
    value=body[:position]
    if meta['format']=='dotenv':
        value=value.replace('\r\n','\n').replace('\r','\n')
        return value.replace('\\n','\n').replace('\\r','\r') if quote=='\"' else value
    if quote=="'": return value
    return re.sub(r'\\([\\\"])',r'\1',value)

def encode(value):
    if '\x00' in value: raise RuntimeError()
    if meta['format']=='dotenv':
        # dotenv does not unescape JSON quotes or backslashes like systemd.
        # Use a literal delimiter; reject unsupported values before writing.
        if '\r' in value: raise RuntimeError()
        for quote in ("'",chr(96),'\"'):
            if quote not in value and not value.endswith('\\'):
                if quote=='\"' and ('\\n' in value or '\\r' in value): continue
                return quote+value+quote
        raise RuntimeError()
    return '\"'+value.replace('\\','\\\\').replace('\"','\\\"')+'\"'

def write(content):
    directory=os.path.dirname(path)
    os.makedirs(directory,mode=0o700,exist_ok=True)
    descriptor,temporary=tempfile.mkstemp(prefix='.keyper-',dir=directory)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8',newline='') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
        os.chmod(path,0o600)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise

content=load()
lines,found=records(content)
entry=next((item for item in found if item[0]==name),None)
if op=='list':
    sys.stdout.write(json.dumps(sorted(item[0] for item in found)))
elif op=='read':
    if not entry: raise RuntimeError()
    sys.stdout.buffer.write(decode(entry[3],lines,entry[1],entry[2]).encode())
elif op=='write':
    if entry and not meta['overwrite']: raise RuntimeError()
    assignment=name+'='+encode(sys.stdin.buffer.read().decode())
    if entry: lines[entry[1]:entry[2]+1]=[assignment]
    else: lines.append(assignment)
    write('\n'.join(lines).rstrip('\n')+'\n')
elif op=='remove':
    if not entry: raise RuntimeError()
    del lines[entry[1]:entry[2]+1]
    output='\n'.join(lines).strip('\n')
    write(output+'\n' if output else '')
else: raise RuntimeError()
`;

const REMOTE_BOOTSTRAP = `python3 -c "import base64;exec(base64.b64decode('${Buffer.from(REMOTE_SCRIPT).toString("base64")}'))"`;

export class SshProvider implements KeyProvider {
  readonly kind = "ssh" as const;

  constructor(private readonly config: SshAlias) {}

  private async run(op: string, name?: string, input?: Buffer, overwrite = false): Promise<Buffer> {
    const metadata = Buffer.from(JSON.stringify({
      path: this.config.path,
      format: this.config.format,
      op,
      ...(name ? { name } : {}),
      overwrite
    })).toString("base64url");
    return await runSafeProcess(
      "ssh",
      [this.config.host, `${REMOTE_BOOTSTRAP} '${metadata}'`],
      input ? { input } : {}
    );
  }

  async list(): Promise<InventoryEntry[]> {
    const output = await this.run("list");
    try {
      const names = JSON.parse(output.toString("utf8")) as unknown;
      if (!Array.isArray(names) || names.some((name) => typeof name !== "string")) {
        throw new KeyperError("PROVIDER_FAILED");
      }
      return names.map((name) => ({ name, portability: "portable" as const }));
    } catch {
      throw new KeyperError("PROVIDER_FAILED");
    } finally {
      output.fill(0);
    }
  }

  async read(name: string): Promise<Buffer> {
    if (!(await this.list()).some((entry) => entry.name === name)) throw new KeyperError("SOURCE_MISSING");
    return await this.run("read", name);
  }

  async write(name: string, value: Buffer, options: WriteOptions): Promise<{ verifiable: boolean }> {
    const exists = (await this.list()).some((entry) => entry.name === name);
    if (exists && !options.overwrite) throw new KeyperError("DESTINATION_EXISTS");
    const output = await this.run("write", name, value, options.overwrite);
    output.fill(0);
    return { verifiable: true };
  }

  async remove(name: string): Promise<void> {
    const output = await this.run("remove", name);
    output.fill(0);
  }

  async doctor(): Promise<void> {
    const output = await this.run("list");
    output.fill(0);
  }
}
