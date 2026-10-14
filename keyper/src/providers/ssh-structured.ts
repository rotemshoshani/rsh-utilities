import { KeyperError } from "../errors.js";
import { runSafeProcess } from "../safe-process.js";
import type { InventoryEntry, KeyProvider, SshStructuredAlias, WriteOptions } from "../types.js";

export const REMOTE_STRUCTURED_SCRIPT = String.raw`
import base64,json,os,stat,sys,tempfile

meta=json.loads(base64.urlsafe_b64decode(sys.argv[1]+'==='))
path,op,name,fields=meta['path'],meta['op'],meta.get('name'),meta['fields']

def parts(pointer):
    result=[part.replace('~1','/').replace('~0','~') for part in pointer[1:].split('/')]
    if any(not part or part in ('__proto__','prototype','constructor') for part in result): raise RuntimeError()
    return result

def load():
    st=os.lstat(path)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode): raise RuntimeError()
    with open(path,'r',encoding='utf-8') as handle:
        value=json.load(handle)
    if not isinstance(value,dict): raise RuntimeError()
    return value

def locate(root,pointer,create=False):
    path_parts=parts(pointer)
    key=path_parts.pop()
    parent=root
    for part in path_parts:
        value=parent.get(part)
        if value is None and create:
            value={}
            parent[part]=value
        if value is None and not create: return None,key,False
        if not isinstance(value,dict): raise RuntimeError()
        parent=value
    return parent,key,key in parent

def write(root):
    directory=os.path.dirname(path)
    descriptor,temporary=tempfile.mkstemp(prefix='.keyper-',dir=directory)
    try:
        os.fchmod(descriptor,0o600)
        with os.fdopen(descriptor,'w',encoding='utf-8') as handle:
            json.dump(root,handle,ensure_ascii=False,indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary,path)
        os.chmod(path,0o600)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise

root=load()
if op=='list':
    names=[]
    for field,pointer in fields.items():
        parent,key,exists=locate(root,pointer)
        if exists: names.append(field)
    sys.stdout.write(json.dumps(sorted(names)))
else:
    if name not in fields: raise RuntimeError()
    parent,key,exists=locate(root,fields[name],op=='write')
    if op=='read':
        if not exists: raise RuntimeError()
        value=parent[key]
        if isinstance(value,str): sys.stdout.buffer.write(value.encode())
        else: sys.stdout.write(json.dumps(value,separators=(',',':')))
    elif op=='write':
        if exists and not meta['overwrite']: raise RuntimeError()
        raw=sys.stdin.buffer.read().decode()
        if meta['valueType']=='boolean':
            if raw not in ('true','false'): raise RuntimeError()
            value=raw=='true'
        elif meta['valueType']=='json': value=json.loads(raw)
        else: value=raw
        parent[key]=value
        write(root)
    elif op=='remove':
        if not exists: raise RuntimeError()
        del parent[key]
        write(root)
    else: raise RuntimeError()
`;

const REMOTE_BOOTSTRAP = `python3 -c "import base64;exec(base64.b64decode('${Buffer.from(REMOTE_STRUCTURED_SCRIPT).toString("base64")}'))"`;

export class SshStructuredProvider implements KeyProvider {
  readonly kind = "ssh-structured" as const;

  constructor(private readonly config: SshStructuredAlias) {}

  private async run(op: string, name?: string, input?: Buffer, options?: WriteOptions): Promise<Buffer> {
    const metadata = Buffer.from(JSON.stringify({
      path: this.config.path,
      fields: this.config.fields,
      op,
      ...(name ? { name } : {}),
      overwrite: options?.overwrite ?? false,
      valueType: options?.valueType ?? "string"
    })).toString("base64url");
    return await runSafeProcess("ssh", [this.config.host, `${REMOTE_BOOTSTRAP} '${metadata}'`], input ? { input } : {});
  }

  async list(): Promise<InventoryEntry[]> {
    const output = await this.run("list");
    try {
      const names = JSON.parse(output.toString("utf8")) as unknown;
      if (!Array.isArray(names) || names.some((name) => typeof name !== "string")) throw new Error();
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
    const output = await this.run("write", name, value, options);
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
