#!/bin/bash

# Build the Docker image
docker build -t egg-sandbox -f ./Dockerfile .

# Verify the image was created
docker images | grep egg-sandbox

