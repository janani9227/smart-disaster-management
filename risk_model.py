import os
import pickle


MODEL_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "flood_model.joblib"
)


# Load the already-trained model.
# No training happens here.
with open(MODEL_PATH, "rb") as f:
    saved_model = pickle.load(f)


tree = saved_model["tree"]
classes = saved_model["classes"]


def predict_one(node, row):

    if node.value is not None:
        return node.value

    if row[node.feature] <= node.threshold:
        return predict_one(node.left, row)

    return predict_one(node.right, row)


def predict_flood_risk(
    duration,
    area_affected,
    human_fatality,
    human_injured,
    human_displaced,
    animal_fatality
):

    row = [
        float(duration),
        float(area_affected),
        float(human_fatality),
        float(human_injured),
        float(human_displaced),
        float(animal_fatality)
    ]

    prediction = predict_one(tree, row)

    try:
        return str(classes[int(prediction)])
    except (ValueError, TypeError, IndexError):
        return str(prediction)