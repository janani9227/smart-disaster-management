
import pandas as pd
import numpy as np
import os
import pickle
from collections import Counter

# ============================================================
# SMART FLOOD DISASTER MANAGEMENT
# CUSTOM DECISION TREE ML MODEL
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DATA_FILE = os.path.join(
    BASE_DIR,
    "India_Flood_Inventory_v3.csv"
)

MODEL_FILE = os.path.join(
    BASE_DIR,
    "flood_model.joblib"
)

print("=" * 70)
print("SMART FLOOD DISASTER MANAGEMENT - ML TRAINING")
print("=" * 70)

# ------------------------------------------------------------
# 1. LOAD REAL INDIA FLOOD INVENTORY
# ------------------------------------------------------------

df = pd.read_csv(DATA_FILE)

print("\nOriginal dataset:", df.shape)

# ------------------------------------------------------------
# 2. SELECT NUMERIC FLOOD-IMPACT FEATURES
# ------------------------------------------------------------

feature_columns = [
    "Duration(Days)",
    "Area Affected",
    "Human fatality",
    "Human injured",
    "Human Displaced",
    "Animal Fatality"
]

for col in feature_columns:
    df[col] = pd.to_numeric(
        df[col],
        errors="coerce"
    )

df[feature_columns] = df[feature_columns].fillna(0)

# ------------------------------------------------------------
# 3. CREATE RISK LABEL
# ------------------------------------------------------------
#
# This is a flood-impact severity classification.
#
# LOW    = lower recorded impact
# MEDIUM = moderate recorded impact
# HIGH   = high recorded impact
#
# ------------------------------------------------------------

def calculate_risk(row):

    score = 0

    score += min(row["Duration(Days)"] / 5, 2)

    score += min(row["Area Affected"] / 100, 2)

    score += min(row["Human fatality"] / 5, 3)

    score += min(row["Human injured"] / 20, 1)

    score += min(row["Human Displaced"] / 1000, 2)

    score += min(row["Animal Fatality"] / 20, 1)

    if score >= 5:
        return 2

    elif score >= 2.5:
        return 1

    return 0


df["Risk"] = df.apply(
    calculate_risk,
    axis=1
)

X = df[feature_columns].values
y = df["Risk"].values

print("Training rows:", len(X))

print("\nClass distribution:")
print(Counter(y))

# ------------------------------------------------------------
# 4. TRAIN / TEST SPLIT
# ------------------------------------------------------------

np.random.seed(42)

indices = np.arange(len(X))
np.random.shuffle(indices)

split = int(len(indices) * 0.80)

train_idx = indices[:split]
test_idx = indices[split:]

X_train = X[train_idx]
y_train = y[train_idx]

X_test = X[test_idx]
y_test = y[test_idx]

# ------------------------------------------------------------
# 5. CUSTOM DECISION TREE
# ------------------------------------------------------------

class Node:

    def __init__(
        self,
        feature=None,
        threshold=None,
        left=None,
        right=None,
        value=None
    ):

        self.feature = feature
        self.threshold = threshold
        self.left = left
        self.right = right
        self.value = value


def gini(y):

    if len(y) == 0:
        return 0

    counts = Counter(y)

    impurity = 1.0

    for count in counts.values():

        probability = count / len(y)

        impurity -= probability ** 2

    return impurity


def best_split(X, y):

    best_gain = 0
    best_feature = None
    best_threshold = None

    parent_gini = gini(y)

    n_features = X.shape[1]

    for feature in range(n_features):

        values = np.unique(X[:, feature])

        if len(values) > 50:

            values = np.percentile(
                values,
                np.linspace(0, 100, 50)
            )

        for threshold in values:

            left_mask = X[:, feature] <= threshold
            right_mask = ~left_mask

            if left_mask.sum() == 0:
                continue

            if right_mask.sum() == 0:
                continue

            left_y = y[left_mask]
            right_y = y[right_mask]

            weighted_gini = (
                len(left_y) / len(y) * gini(left_y)
                +
                len(right_y) / len(y) * gini(right_y)
            )

            gain = parent_gini - weighted_gini

            if gain > best_gain:

                best_gain = gain
                best_feature = feature
                best_threshold = threshold

    return (
        best_feature,
        best_threshold,
        best_gain
    )


def build_tree(X, y, depth=0, max_depth=6):

    if (
        len(y) == 0
        or depth >= max_depth
        or len(set(y)) == 1
    ):

        majority = Counter(y).most_common(1)[0][0]

        return Node(value=int(majority))

    feature, threshold, gain = best_split(X, y)

    if feature is None or gain <= 0:

        majority = Counter(y).most_common(1)[0][0]

        return Node(value=int(majority))

    left_mask = X[:, feature] <= threshold
    right_mask = ~left_mask

    left_tree = build_tree(
        X[left_mask],
        y[left_mask],
        depth + 1,
        max_depth
    )

    right_tree = build_tree(
        X[right_mask],
        y[right_mask],
        depth + 1,
        max_depth
    )

    return Node(
        feature=feature,
        threshold=threshold,
        left=left_tree,
        right=right_tree
    )


def predict_one(node, row):

    if node.value is not None:

        return node.value

    if row[node.feature] <= node.threshold:

        return predict_one(
            node.left,
            row
        )

    return predict_one(
        node.right,
        row
    )


def predict_tree(node, X):

    return np.array([
        predict_one(node, row)
        for row in X
    ])


# ------------------------------------------------------------
# 6. TRAIN
# ------------------------------------------------------------

print("\nTraining custom Decision Tree...")

tree = build_tree(
    X_train,
    y_train,
    max_depth=6
)

# ------------------------------------------------------------
# 7. EVALUATE
# ------------------------------------------------------------

predictions = predict_tree(
    tree,
    X_test
)

accuracy = np.mean(
    predictions == y_test
)

print("\n" + "=" * 70)
print("MODEL RESULTS")
print("=" * 70)

print(
    f"\nAccuracy: {accuracy * 100:.2f}%"
)

for class_id, name in [
    (0, "LOW"),
    (1, "MEDIUM"),
    (2, "HIGH")
]:

    actual = y_test == class_id
    predicted = predictions == class_id

    true_positive = np.sum(
        actual & predicted
    )

    false_positive = np.sum(
        (~actual) & predicted
    )

    false_negative = np.sum(
        actual & (~predicted)
    )

    precision = (
        true_positive /
        (true_positive + false_positive)
        if true_positive + false_positive > 0
        else 0
    )

    recall = (
        true_positive /
        (true_positive + false_negative)
        if true_positive + false_negative > 0
        else 0
    )

    f1 = (
        2 * precision * recall /
        (precision + recall)
        if precision + recall > 0
        else 0
    )

    print(
        f"{name}: "
        f"Precision={precision:.3f}, "
        f"Recall={recall:.3f}, "
        f"F1={f1:.3f}"
    )

# ------------------------------------------------------------
# 8. SAVE MODEL
# ------------------------------------------------------------

model_package = {
    "tree": tree,
    "features": feature_columns,
    "classes": {
        0: "LOW",
        1: "MEDIUM",
        2: "HIGH"
    }
}

with open(
    MODEL_FILE,
    "wb"
) as file:

    pickle.dump(
        model_package,
        file
    )

print("\nModel saved as:")
print(MODEL_FILE)

print("\n" + "=" * 70)
print("ML TRAINING COMPLETE")
print("=" * 70)

