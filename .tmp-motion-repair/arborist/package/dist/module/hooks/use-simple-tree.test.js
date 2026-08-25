import { act, renderHook } from "@testing-library/react";
import { useSimpleTree } from "./use-simple-tree";
/* onCreate has to write a new node's id (and a folder's children) under a key
   the accessors will read back. A function accessor can't be inverted to a key,
   so creation with one must fail fast rather than return an unusable node
   (issue #73 review follow-up). */
describe("useSimpleTree onCreate guards function accessors", () => {
    function controllerFor(data, options) {
        const { result } = renderHook(() => useSimpleTree(data, options));
        return result.current[1];
    }
    const create = { parentId: null, parentNode: null, index: 0 };
    test("throws when idAccessor is a function", () => {
        const controller = controllerFor([{ uuid: "1", name: "a" }], { idAccessor: (d) => d.uuid });
        expect(() => controller.onCreate(Object.assign(Object.assign({}, create), { type: "leaf" }))).toThrow(/idAccessor is a function/);
    });
    test("throws when creating a folder with a function childrenAccessor", () => {
        const controller = controllerFor([{ id: "1", name: "a" }], {
            childrenAccessor: (d) => d.kids,
        });
        expect(() => controller.onCreate(Object.assign(Object.assign({}, create), { type: "internal" }))).toThrow(/childrenAccessor is a function/);
    });
    test("a leaf can still be created when only childrenAccessor is a function", () => {
        const controller = controllerFor([{ id: "1", name: "a" }], {
            childrenAccessor: (d) => d.kids,
        });
        // onCreate calls setData, so run it inside act to keep the suite warning-clean.
        expect(() => act(() => void controller.onCreate(Object.assign(Object.assign({}, create), { type: "leaf" })))).not.toThrow();
    });
});
/* A single onChange callback lets callers persist the whole tree after any
   internal mutation, without wiring up each handler themselves (#302). */
describe("useSimpleTree onChange fires on mutations with the latest data (#302)", () => {
    const initial = [
        { id: "a", name: "a" },
        { id: "b", name: "b" },
    ];
    function setup() {
        const onChange = jest.fn();
        const { result } = renderHook(() => useSimpleTree(initial, { onChange }));
        return { onChange, data: () => result.current[0], controller: () => result.current[1] };
    }
    test("does not fire on mount", () => {
        const { onChange } = setup();
        expect(onChange).not.toHaveBeenCalled();
    });
    test("fires on rename with the exact array the hook now returns", () => {
        var _a;
        const { onChange, data, controller } = setup();
        act(() => controller().onRename({ id: "a", name: "renamed", node: null }));
        expect(onChange).toHaveBeenCalledTimes(1);
        const arg = onChange.mock.calls[0][0];
        expect((_a = arg.find((n) => n.id === "a")) === null || _a === void 0 ? void 0 : _a.name).toBe("renamed");
        // onChange receives the same reference committed to state, not a fresh copy.
        expect(data()).toBe(arg);
    });
    test("fires on move with the reordered data", () => {
        const { onChange, controller } = setup();
        act(() => controller().onMove({
            dragIds: ["a"],
            parentId: null,
            index: 2,
            dragNodes: [],
            parentNode: null,
        }));
        expect(onChange.mock.calls[0][0].map((n) => n.id)).toEqual(["b", "a"]);
    });
    test("fires on delete with the remaining data", () => {
        const { onChange, controller } = setup();
        act(() => controller().onDelete({ ids: ["a"], nodes: [] }));
        expect(onChange.mock.calls[0][0].map((n) => n.id)).toEqual(["b"]);
    });
    test("fires on create with the grown data", () => {
        const { onChange, controller } = setup();
        act(() => void controller().onCreate({ parentId: null, parentNode: null, index: 0, type: "leaf" }));
        expect(onChange).toHaveBeenCalledTimes(1);
        expect(onChange.mock.calls[0][0]).toHaveLength(3);
    });
});
