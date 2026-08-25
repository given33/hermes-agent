var __awaiter = (this && this.__awaiter) || function (thisArg, _arguments, P, generator) {
    function adopt(value) { return value instanceof P ? value : new P(function (resolve) { resolve(value); }); }
    return new (P || (P = Promise))(function (resolve, reject) {
        function fulfilled(value) { try { step(generator.next(value)); } catch (e) { reject(e); } }
        function rejected(value) { try { step(generator["throw"](value)); } catch (e) { reject(e); } }
        function step(result) { result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected); }
        step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
};
import { jsx as _jsx } from "react/jsx-runtime";
import { createRef } from "react";
import { act, fireEvent, render, screen } from "@testing-library/react";
import { Tree } from "./tree";
const data = [
    {
        id: "1",
        name: "root",
        children: [
            { id: "2", name: "a" },
            { id: "3", name: "b", children: [{ id: "4", name: "c" }] },
        ],
    },
];
/* Selecting a row kicks off tree.scrollTo(), whose promise resolves on a
   microtask after fireEvent's synchronous act() scope has exited — the
   resulting List scrollToItem() update would otherwise warn about not being
   wrapped in act(). Awaiting an async act flushes that trailing update. */
function click(el, init) {
    return __awaiter(this, void 0, void 0, function* () {
        yield act(() => __awaiter(this, void 0, void 0, function* () {
            fireEvent.click(el, init);
        }));
    });
}
/* #303: multi-select should respond to Ctrl+Click (Windows) as well as
   Cmd/Meta+Click (macOS). */
test("Ctrl+Click adds a row to the selection (#303)", () => __awaiter(void 0, void 0, void 0, function* () {
    render(_jsx(Tree, { data: data, openByDefault: true }));
    const [, a, b] = screen.getAllByRole("treeitem");
    yield click(a);
    expect(a.getAttribute("aria-selected")).toBe("true");
    yield click(b, { ctrlKey: true });
    expect(a.getAttribute("aria-selected")).toBe("true");
    expect(b.getAttribute("aria-selected")).toBe("true");
}));
test("Ctrl+Click toggles an already-selected row off (#303)", () => __awaiter(void 0, void 0, void 0, function* () {
    render(_jsx(Tree, { data: data, openByDefault: true }));
    const [, a, b] = screen.getAllByRole("treeitem");
    yield click(a);
    yield click(b, { ctrlKey: true });
    yield click(b, { ctrlKey: true });
    expect(a.getAttribute("aria-selected")).toBe("true");
    expect(b.getAttribute("aria-selected")).toBe("false");
}));
test("Ctrl+Click falls through to a plain select when multi-select is disabled (#303)", () => __awaiter(void 0, void 0, void 0, function* () {
    render(_jsx(Tree, { data: data, openByDefault: true, disableMultiSelection: true }));
    const [, a, b] = screen.getAllByRole("treeitem");
    yield click(a);
    yield click(b, { ctrlKey: true });
    expect(a.getAttribute("aria-selected")).toBe("false");
    expect(b.getAttribute("aria-selected")).toBe("true");
}));
/* #10: a row's background/selection highlight must span the full scrollable
   width, not stop at the viewport edge, when content overflows horizontally. */
test("rows get min-width: max-content so the highlight spans overflow (#10)", () => {
    render(_jsx(Tree, { data: data, openByDefault: true }));
    for (const row of screen.getAllByRole("treeitem")) {
        expect(row.style.minWidth).toBe("max-content");
    }
});
/* #325: forward an accessible name and multiselectable state onto the
   role="tree" element. */
test("forwards aria-label to the role=tree element (#325)", () => {
    render(_jsx(Tree, { data: data, "aria-label": "File explorer" }));
    expect(screen.getByRole("tree").getAttribute("aria-label")).toBe("File explorer");
});
test("forwards aria-labelledby to the role=tree element (#325)", () => {
    render(_jsx(Tree, { data: data, "aria-labelledby": "heading-id" }));
    expect(screen.getByRole("tree").getAttribute("aria-labelledby")).toBe("heading-id");
});
test("marks the tree aria-multiselectable by default (#325)", () => {
    render(_jsx(Tree, { data: data }));
    expect(screen.getByRole("tree").getAttribute("aria-multiselectable")).toBe("true");
});
test("omits aria-multiselectable when multi-select is disabled (#325)", () => {
    render(_jsx(Tree, { data: data, disableMultiSelection: true }));
    expect(screen.getByRole("tree").hasAttribute("aria-multiselectable")).toBe(false);
});
/* #245, #308: clicking the empty area below the rows clears the selection by
   default; disableDeselectOnClick opts out of that. The deselect handler lives
   on the list's outer (scroll) element, which is wired to tree.listEl. */
test("clicking empty tree space clears the selection by default (#245)", () => __awaiter(void 0, void 0, void 0, function* () {
    const ref = createRef();
    render(_jsx(Tree, { data: data, openByDefault: true, ref: ref }));
    const [, a] = screen.getAllByRole("treeitem");
    yield click(a);
    expect(a.getAttribute("aria-selected")).toBe("true");
    yield click(ref.current.listEl.current);
    expect(a.getAttribute("aria-selected")).toBe("false");
}));
test("disableDeselectOnClick keeps the selection when empty space is clicked (#245, #308)", () => __awaiter(void 0, void 0, void 0, function* () {
    const ref = createRef();
    render(_jsx(Tree, { data: data, openByDefault: true, disableDeselectOnClick: true, ref: ref }));
    const [, a] = screen.getAllByRole("treeitem");
    yield click(a);
    expect(a.getAttribute("aria-selected")).toBe("true");
    yield click(ref.current.listEl.current);
    expect(a.getAttribute("aria-selected")).toBe("true");
}));
/* #257: an <input> rendered inside the tree (e.g. in a modal) must still
   receive Space keystrokes. The container's keydown handler calls
   preventDefault on Space to toggle/select, which would otherwise swallow the
   character as the event bubbles up from the nested input. */
test("does not preventDefault Space typed into a nested input (#257)", () => {
    render(_jsx(Tree, { data: data, openByDefault: true, children: () => (_jsx("div", { children: _jsx("input", { "aria-label": "modal-input" }) })) }));
    const [input] = screen.getAllByLabelText("modal-input");
    const notPrevented = fireEvent.keyDown(input, { key: " ", code: "Space" });
    // fireEvent returns false when a listener called preventDefault.
    expect(notPrevented).toBe(true);
});
test("does not preventDefault Space typed into a nested contenteditable (#257)", () => {
    render(_jsx(Tree, { data: data, openByDefault: true, children: () => (_jsx("div", { children: _jsx("div", { "aria-label": "editable", contentEditable: true, suppressContentEditableWarning: true }) })) }));
    const [editable] = screen.getAllByLabelText("editable");
    const notPrevented = fireEvent.keyDown(editable, { key: " ", code: "Space" });
    expect(notPrevented).toBe(true);
});
test("still calls preventDefault on Space typed on the tree container itself (#257)", () => {
    render(_jsx(Tree, { data: data, openByDefault: true }));
    const tree = screen.getByRole("tree");
    const notPrevented = fireEvent.keyDown(tree, { key: " ", code: "Space" });
    expect(notPrevented).toBe(false);
});
