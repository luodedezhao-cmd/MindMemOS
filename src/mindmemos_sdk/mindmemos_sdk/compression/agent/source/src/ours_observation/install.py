"""Install native meter after/before bridge in an isolated Ours container only."""
from pathlib import Path
from src.host_observation.install import install as install_host


def install(root=Path('/usr/lib/node_modules/openclaw')):
    install_host(root)
    dist = root / 'node_modules/@mariozechner/pi-ai/dist'
    stream = dist / 'stream.js'
    source = stream.read_text()
    for name in ('stream', 'streamSimple'):
        old = f'observe(() => provider.{name}(model, context, options), model, context, options)'
        new = f'observe(observedOptions => provider.{name}(model, context, observedOptions), model, context, options)'
        if old not in source and new not in source:
            raise ValueError('Unsupported Ours stream anchor')
        source = source.replace(old, new)
    # install_host refreshes host-meter; preserve that implementation for bypass calls.
    (dist / 'host-meter-base.mjs').write_text((dist / 'host-meter.mjs').read_text())
    (dist / 'host-meter.mjs').write_text(Path(__file__).with_name('meter.mjs').read_text())
    stream.write_text(source)


if __name__ == '__main__':
    install()
