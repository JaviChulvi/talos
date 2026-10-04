// Retain installed dependency notices beside the minified browser assets.
// Include build dependencies too: transformed CSS and bundled tooling can contribute code.
import { existsSync, readdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

const visited = new Set();
const packages = new Map();
function visit(directory) {
  const path = realpathSync(directory);
  if (visited.has(path)) return;
  visited.add(path);
  const entries = readdirSync(path, { withFileTypes: true });
  if (entries.some(entry => entry.name === 'package.json')) {
    const pkg = JSON.parse(readFileSync(join(path, 'package.json'), 'utf8'));
    if (pkg.name && pkg.version) {
      const notices = entries.filter(entry => entry.isFile() && /^(licen[sc]e|copying|notice)([.-]|$)/i.test(entry.name))
        .map(entry => ({ file: entry.name, text: readFileSync(join(path, entry.name), 'utf8') }));
      if (!notices.length) {
        const readme = entries.find(entry => entry.isFile() && /^readme/i.test(entry.name));
        if (readme) {
          const text = readFileSync(join(path, readme.name), 'utf8');
          if (/copyright/i.test(text)) notices.push({file: readme.name, text});
        }
      }
      const supplementalName = pkg.name.startsWith("@rolldown/binding-") ? "_rolldown_binding" : pkg.name.replace(/[^a-zA-Z0-9.-]/g, "_");
      const supplement = `../licenses/frontend/${supplementalName}-${pkg.version}.txt`;
      if (!notices.length && existsSync(supplement)) notices.push({file: supplement, text: readFileSync(supplement, "utf8")});
      const key = `${pkg.name}@${pkg.version}`;
      const previous = packages.get(key);
      if (!previous || notices.length > previous.notices.length) packages.set(key, {
        name: pkg.name, version: pkg.version,
        origin: pkg.repository?.url || pkg.repository || pkg.homepage || `https://www.npmjs.com/package/${pkg.name}/v/${pkg.version}`,
        license: pkg.license || pkg.licenses || 'NOASSERTION',
        modifications: 'No source edits; browser code may be bundled and minified.', notices,
      });
    }
  }
  for (const entry of entries) {
    if (entry.isDirectory() || entry.isSymbolicLink()) {
      try { visit(join(path, entry.name)); } catch (error) {
        if (!['ENOTDIR', 'ENOENT'].includes(error.code)) throw error;
      }
    }
  }
}
visit('node_modules');
const records = [...packages.values()].sort((a, b) => `${a.name}@${a.version}`.localeCompare(`${b.name}@${b.version}`));
if (!records.length) throw new Error('No installed dependency notices were collected');
writeFileSync('dist/dependencies.json', JSON.stringify(records, null, 2) + '\n');
const projectNotices = ['../LICENSE', '../NOTICE', '../licenses/shadcn-ui-LICENSE.txt'].map(path => readFileSync(path, 'utf8')).join('\n\n');
writeFileSync('dist/third-party-notices.txt', projectNotices + '\n\n' + records.map(pkg =>
  `${pkg.name}@${pkg.version}\nOrigin: ${pkg.origin}\nLicense: ${JSON.stringify(pkg.license)}\n${pkg.modifications}\n` +
  (pkg.notices.length ? pkg.notices.map(n => `${n.file}\n${n.text}`).join('\n') : 'No standalone notice found; consult the dependency inventory before redistribution.')
).join('\n\n' + '='.repeat(72) + '\n\n'));
console.log(`Retained notices for ${records.length} installed frontend packages`);
