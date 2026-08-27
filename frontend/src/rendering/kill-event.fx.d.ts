import * as THREE from 'three';
export declare class KillEventFX {
    private group;
    private effects;
    private now;
    constructor(parent: THREE.Scene, clock?: () => number);
    trigger(uid: string, lat: number, lon: number, alt: number, referenceLat: number, referenceLon: number): void;
    tick(): void;
    dispose(): void;
}
//# sourceMappingURL=kill-event.fx.d.ts.map