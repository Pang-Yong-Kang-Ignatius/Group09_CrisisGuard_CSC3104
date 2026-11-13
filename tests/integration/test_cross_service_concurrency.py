
import concurrent.futures
import uuid

import requests


BASE_URLS = {
    "patient": "http://localhost:8001/events",
    "emergency": "http://localhost:8003/events",
    "admin": "http://localhost:8004/events",
}

RUN_ID = uuid.uuid4().hex


def make_event(service, index):
    event_types = {
        "patient": "patient_registration",
        "emergency": "emergency_record",
        "admin": "patient_discharge",
    }

    return {
        "event_id": f"CROSS_CONCURRENT_{RUN_ID}_{service}_{index}",
        "site_id": "Hospital_A",
        "event_type": event_types[service],
        "base_priority": 2,
        "prerequisite_ids": [],
        "payload": {
            "patient_id": f"TEST_{RUN_ID}_{index}",
            "test": "cross_service_concurrency",
        },
    }


def submit_event(service, index):
    try:
        response = requests.post(
            BASE_URLS[service],
            json=make_event(service, index),
            timeout=15,
        )

        return {
            "service": service,
            "status": response.status_code,
            "result": response.json().get("result"),
        }

    except requests.RequestException as exc:
        return {
            "service": service,
            "status": "ERROR",
            "result": str(exc),
        }


def main():
    tasks = [
        (service, index)
        for service in BASE_URLS
        for index in range(10)
    ]

    print("Sending 30 concurrent requests...\n")

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=30
    ) as executor:
        results = list(
            executor.map(
                lambda task: submit_event(*task),
                tasks,
            )
        )

    for result in results:
        print(
            f"{result['service']:10} "
            f"HTTP {result['status']} "
            f"{result['result']}"
        )

    successful = sum(
        r["status"] == 201
        and r["result"] == "applied"
        for r in results
    )

    print("\n--- SUMMARY ---")
    print(f"New events applied: {successful}/30")
    print(f"Other responses: {30 - successful}")
    print(f"Test run ID: {RUN_ID}")

    assert successful == 30, (
        "Expected all 30 independent events to be applied"
    )

    print("\nCROSS-SERVICE CONCURRENCY TEST PASSED")


if __name__ == "__main__":
    main()
