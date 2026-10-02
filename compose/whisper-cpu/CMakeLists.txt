# wcpp-worker: the decoder process of compose/whisper-cpu/server.py, linked statically against
# whisper.cpp at the commit the Dockerfile checks out. CMake carries whisper's own link
# dependencies (ggml, the CPU backend, KleidiAI, OpenMP), so nothing here lists them by hand.
cmake_minimum_required(VERSION 3.20)
project(wcpp_worker C CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT WHISPER_CPP_DIR)
    message(FATAL_ERROR "-DWHISPER_CPP_DIR=<whisper.cpp checkout> is required")
endif()

add_subdirectory(${WHISPER_CPP_DIR} whisper.cpp)

add_executable(wcpp-worker wcpp_worker.cpp)
# nlohmann/json, which whisper.cpp vendors for its own server example.
target_include_directories(wcpp-worker PRIVATE ${WHISPER_CPP_DIR}/examples)
target_link_libraries(wcpp-worker PRIVATE whisper)
