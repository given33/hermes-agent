import { SimpleTreeOptions } from "../data/simple-tree";
import { CreateHandler, DeleteHandler, MoveHandler, RenameHandler } from "../types/handlers";
export type SimpleTreeData = {
    id: string;
    name: string;
    children?: SimpleTreeData[];
};
export type UseSimpleTreeOptions<T> = SimpleTreeOptions<T> & {
    onChange?: (data: readonly T[]) => void;
};
export declare function useSimpleTree<T>(initialData: readonly T[], options?: UseSimpleTreeOptions<T>): readonly [readonly T[], {
    onMove: MoveHandler<T>;
    onRename: RenameHandler<T>;
    onCreate: CreateHandler<T>;
    onDelete: DeleteHandler<T>;
}];
