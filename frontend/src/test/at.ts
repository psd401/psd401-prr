// ABOUTME: Indexed access for tests under noUncheckedIndexedAccess.
// ABOUTME: A missing element fails the test naming the index, not with a later TypeError.
export function at<T>(items: ArrayLike<T>, index: number): T {
  const item = items[index]
  if (item === undefined) {
    throw new Error(`expected an element at index ${index}; there are ${items.length}`)
  }
  return item
}
