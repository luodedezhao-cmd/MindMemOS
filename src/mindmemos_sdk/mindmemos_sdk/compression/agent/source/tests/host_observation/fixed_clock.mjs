// Test fixture only: native system prompts include wall-clock time. Freeze it
// for exact before/after comparisons; timers and performance.now stay real.
const NativeDate = globalThis.Date;
const instant = NativeDate.parse('2026-09-14T00:00:00.000Z');
globalThis.Date = new Proxy(NativeDate, {
  construct(target, args, newTarget) {return Reflect.construct(target, args.length ? args : [instant], newTarget);},
  apply() {return new NativeDate(instant).toString();},
  get(target, key) {return key === 'now' ? () => instant : Reflect.get(target,key);},
});
