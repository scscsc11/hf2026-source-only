import * as THREE from 'three';
export interface ZonePolygon {
    /** [(lat, lon), ...] — at least 3 vertices for a filled mesh. */
    polygon: Array<[number, number]>;
    alt_min: number;
    alt_max: number;
}
export interface ZoneOverlayConfig {
    /** Edge / fill colour (hex). */
    color: number;
    /** Fill opacity 0..1 (the bottom face uses alpha*0.5). */
    alpha: number;
    /** Kept for config compat; the prism wireframe always draws edges. */
    showBoundary?: boolean;
    /** Group name (debug visibility in the scene graph). */
    name?: string;
    /** Default ceiling (metres) when a zone omits alt_max. */
    defaultAltMax?: number;
    /** Floating Chinese label shown above the volume (023). */
    label?: string;
}
export declare class ZoneOverlay {
    private group;
    private children;
    private readonly config;
    constructor(parent: THREE.Scene, config: ZoneOverlayConfig);
    /**
     * Rebuild the region visuals. Each valid (>= 3 vertices) polygon becomes:
     *  (1) a faint bottom fill Mesh (fan triangulation at alt_min) — kept for
     *      the contract tests;
     *  (2) a dense triangular side wireframe: the altitude range is sliced into
     *      many levels and every side quad is triangulated, then WireframeGeometry
     *      exposes all edges (horizontal rings + verticals + diagonals) for a
     *      fine triangle-mesh look like the terrain;
     *  (3) a floating text label sprite above the volume centre.
     */
    update(zones: ZonePolygon[], referenceLat: number, referenceLon: number): void;
    dispose(): void;
    private disposeObject;
}
//# sourceMappingURL=zone-overlay.d.ts.map