/* 内部结构：与 Blender RigidBody 接口的对象布局保持一致。
 *
 * 说明：对外只暴露 rb_abi.h 中的不透明指针；本文件的结构体布局与 Blender
 * `intern/rigidbody/rb_bullet_api.cpp` 的对应结构一致，便于 lab_* 扩展函数
 * 在两套实现间共享语义。 */

#ifndef UNILAB_BULLET_RB_INTERNAL_H
#define UNILAB_BULLET_RB_INTERNAL_H

#include "rb_abi.h"

#include <btBulletDynamicsCommon.h>

#include <BulletCollision/CollisionShapes/btScaledBvhTriangleMeshShape.h>
#include <BulletCollision/Gimpact/btGImpactCollisionAlgorithm.h>
#include <BulletCollision/Gimpact/btGImpactShape.h>
#include <LinearMath/btConvexHullComputer.h>

struct rbVert {
  btScalar x, y, z;
};

struct rbTri {
  int v0, v1, v2;
};

struct rbMeshData {
  btTriangleIndexVertexArray *index_array;
  rbVert *vertices;
  rbTri *triangles;
  int num_vertices;
  int num_triangles;
};

struct rbCollisionShape {
  btCollisionShape *cshape;
  rbMeshData *mesh;
  rbCollisionShape **compound_child_shapes;
  int compound_children;
};

struct rbRigidBody {
  btRigidBody *body;
  int col_groups;
};

struct rbDynamicsWorld {
  btDiscreteDynamicsWorld *dynamics_world;
  btDefaultCollisionConfiguration *collision_configuration;
  btDispatcher *dispatcher;
  btBroadphaseInterface *broadphase;
  btConstraintSolver *constraint_solver;
  btOverlapFilterCallback *filter_callback;
};

inline btVector3 to_bt_vec(const float values[3])
{
  return btVector3(values[0], values[1], values[2]);
}

inline btQuaternion to_bt_quat(const float values[4])
{
  /* Blender 的接口四元数顺序为 (w, x, y, z)。 */
  return btQuaternion(values[1], values[2], values[3], values[0]);
}

inline btTransform to_bt_transform(const float loc[3], const float rot[4])
{
  btTransform transform;
  transform.setIdentity();
  transform.setOrigin(to_bt_vec(loc));
  transform.setRotation(to_bt_quat(rot));
  return transform;
}

inline void from_bt_vec(float out[3], const btVector3 &value)
{
  out[0] = static_cast<float>(value.x());
  out[1] = static_cast<float>(value.y());
  out[2] = static_cast<float>(value.z());
}

inline void from_bt_quat(float out[4], const btQuaternion &value)
{
  out[0] = static_cast<float>(value.w());
  out[1] = static_cast<float>(value.x());
  out[2] = static_cast<float>(value.y());
  out[3] = static_cast<float>(value.z());
}

/* 约束帧：世界坐标下的 pivot/orn 转到两个刚体的局部坐标。 */
inline void make_constraint_frames(btTransform &frame1,
                                   btTransform &frame2,
                                   btRigidBody *body1,
                                   btRigidBody *body2,
                                   const float pivot[3],
                                   const float orn[4])
{
  const btTransform pivot_transform = to_bt_transform(pivot, orn);
  frame1 = body1->getWorldTransform().inverse() * pivot_transform;
  frame2 = body2->getWorldTransform().inverse() * pivot_transform;
}

#endif /* UNILAB_BULLET_RB_INTERNAL_H */
